#!/usr/bin/env python3
"""
dupe-review.py

Interactive terminal tool for reviewing "fuzzy" duplicates in a Plex library — titles that
exist as more than one file (different quality/source/encode), as opposed to the exact
byte-identical duplicates dedupe-library.sh already handles.

Data sources:
  - Plex's own SQLite DB: Plex already ffprobed every file and already grouped same-title
    files as "versions" of one item. We read that instead of re-parsing filenames ourselves.
  - Radarr / Sonarr APIs: for whichever file each app currently tracks, pull the *actual*
    customFormatScore computed against your real quality profile — this reflects preferences
    you already configured, not a guess this script makes up.
  - qBittorrent API: flags a candidate-for-deletion file that is still an active seeding
    torrent, so you don't accidentally trigger a hit-and-run.

This tool never deletes anything on its own. Every group requires an explicit interactive
choice: keep one file, keep several, or keep all (do nothing).
"""

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.panel import Panel
from rich.prompt import Prompt
from rich import box

# ==== CONFIG — edit these for your setup ====
RADARR_URL = "http://localhost:7878"
RADARR_KEY = "CHANGE_ME"
SONARR_URL = "http://localhost:8989"
SONARR_KEY = "CHANGE_ME"
QBIT_URL = "http://localhost:8080"
QBIT_USER = "admin"
QBIT_PASS = "CHANGE_ME"

PLEX_DB = (
    "/plexdata/Plex Media Server/Plug-in Support/Databases/"
    "com.plexapp.plugins.library.db"
)

# Container-path -> host-path prefixes, same mapping used by dedupe-library.sh
PATH_MAP = [
    ("/movies/", "/mnt/user/Media/Movies/"),
    ("/tv/", "/mnt/user/Media/tv/"),
    ("/arr/movies/", "/mnt/user/Media/arr/movies/"),
    ("/arr/shows/", "/mnt/user/Media/arr/shows/"),
]

LOGFILE = "/logs/dupe-review.log"
STATE_FILE = "/logs/dupe-review-state.json"
# ==== END CONFIG ====

console = Console()


def to_host_path(p: str) -> str:
    for prefix, host in PATH_MAP:
        if p.startswith(prefix):
            return host + p[len(prefix):]
    return p


def to_container_variants(host_path: str) -> list[str]:
    variants = [host_path]
    for prefix, host in PATH_MAP:
        if host_path.startswith(host):
            variants.append(prefix + host_path[len(host):])
    return variants


# ---------------------------------------------------------------------------
# Plex DB
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    group_key: str
    group_title: str
    group_kind: str  # "movie" or "episode"
    media_item_id: int
    metadata_item_id: int
    path: str
    size: int
    width: int
    height: int
    bitrate: int
    duration_ms: int
    video_codec: str
    plex_audio_codec: str
    plex_audio_channels: int
    container: str
    color_trc: str
    dovi_present: bool = False
    dovi_profile: str = ""
    audio_streams: list = field(default_factory=list)  # [(codec, channels, bitrate, extra)]
    # enrichment filled in later
    tracked_by: str = "loose"
    release_group: str = ""
    quality_name: str = ""
    custom_formats: list = field(default_factory=list)
    custom_format_score: Optional[int] = None
    media_info: dict = field(default_factory=dict)
    actively_seeding: bool = False


def open_plex_db():
    uri = f"file:{PLEX_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_plex_duplicate_groups(progress, task) -> list[FileRecord]:
    con = open_plex_db()
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute(
        """
        SELECT m.metadata_type, m.id AS metadata_item_id, m.title, m.year,
               m."index" AS ep_index, m.parent_id,
               mi.id AS media_item_id, mi.width, mi.height, mi.bitrate, mi.duration,
               mi.video_codec, mi.audio_codec, mi.audio_channels, mi.container, mi.color_trc,
               mp.file, mp.size
        FROM metadata_items m
        JOIN media_items mi ON mi.metadata_item_id = m.id AND mi.deleted_at IS NULL
        JOIN media_parts mp ON mp.media_item_id = mi.id
        WHERE m.metadata_type IN (1, 4)
          AND m.id IN (
            SELECT metadata_item_id FROM media_items
            WHERE deleted_at IS NULL
            GROUP BY metadata_item_id HAVING COUNT(*) > 1
          )
        ORDER BY m.metadata_type, m.id
        """
    )
    rows = cur.fetchall()
    progress.update(task, total=len(rows) + 2)

    # Episode parent hierarchy (show/season titles) for episode groups
    ep_ids = sorted({r["metadata_item_id"] for r in rows if r["metadata_type"] == 4})
    ep_titles = {}
    if ep_ids:
        placeholders = ",".join("?" * len(ep_ids))
        cur.execute(
            f"""
            SELECT ep.id, show.title AS show_title, show.year AS show_year,
                   season."index" AS season_num, ep."index" AS ep_num, ep.title AS ep_title
            FROM metadata_items ep
            JOIN metadata_items season ON season.id = ep.parent_id
            JOIN metadata_items show ON show.id = season.parent_id
            WHERE ep.id IN ({placeholders})
            """,
            ep_ids,
        )
        for r in cur.fetchall():
            season_num = r["season_num"] if r["season_num"] is not None else 0
            ep_num = r["ep_num"] if r["ep_num"] is not None else 0
            label = f"{r['show_title']} - S{season_num:02d}E{ep_num:02d} - {r['ep_title']}"
            ep_titles[r["id"]] = label
    progress.advance(task)

    # Streams (video HDR/DV + audio) for every media_item involved
    media_item_ids = sorted({r["media_item_id"] for r in rows})
    streams_by_media_item = {}
    if media_item_ids:
        placeholders = ",".join("?" * len(media_item_ids))
        cur.execute(
            f"""
            SELECT media_item_id, stream_type_id, codec, channels, bitrate, extra_data
            FROM media_streams
            WHERE media_item_id IN ({placeholders})
            """,
            media_item_ids,
        )
        for r in cur.fetchall():
            streams_by_media_item.setdefault(r["media_item_id"], []).append(r)
    progress.advance(task)

    con.close()

    records = []
    for r in rows:
        kind = "movie" if r["metadata_type"] == 1 else "episode"
        if kind == "movie":
            group_title = f"{r['title']} ({r['year']})" if r["year"] else r["title"]
        else:
            group_title = ep_titles.get(r["metadata_item_id"], r["title"])
        group_key = f"{kind}:{r['metadata_item_id']}"

        rec = FileRecord(
            group_key=group_key,
            group_title=group_title,
            group_kind=kind,
            media_item_id=r["media_item_id"],
            metadata_item_id=r["metadata_item_id"],
            path=r["file"],
            size=r["size"] or 0,
            width=r["width"] or 0,
            height=r["height"] or 0,
            bitrate=r["bitrate"] or 0,
            duration_ms=r["duration"] or 0,
            video_codec=(r["video_codec"] or "").lower(),
            plex_audio_codec=(r["audio_codec"] or "").lower(),
            plex_audio_channels=r["audio_channels"] or 0,
            container=(r["container"] or "").lower(),
            color_trc=(r["color_trc"] or "").lower(),
        )

        for s in streams_by_media_item.get(r["media_item_id"], []):
            extra = {}
            if s["extra_data"]:
                try:
                    extra = json.loads(s["extra_data"])
                except (ValueError, TypeError):
                    extra = {}
            if s["stream_type_id"] == 1:  # video
                rec.dovi_present = extra.get("ma:DOVIPresent") == "1"
                rec.dovi_profile = extra.get("ma:DOVIProfile", "")
                if not rec.color_trc:
                    rec.color_trc = (extra.get("ma:colorTrc") or "").lower()
            elif s["stream_type_id"] == 2:  # audio
                rec.audio_streams.append(
                    (s["codec"], s["channels"], s["bitrate"], extra)
                )
        progress.advance(task)
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Radarr / Sonarr enrichment
# ---------------------------------------------------------------------------

def load_radarr_index() -> dict:
    r = requests.get(f"{RADARR_URL}/api/v3/movie", headers={"X-Api-Key": RADARR_KEY}, timeout=30)
    r.raise_for_status()
    file_ids = [m["movieFileId"] for m in r.json() if m.get("hasFile") and m.get("movieFileId")]

    index = {}
    for i in range(0, len(file_ids), 150):
        batch = file_ids[i:i + 150]
        rr = requests.get(
            f"{RADARR_URL}/api/v3/moviefile",
            headers={"X-Api-Key": RADARR_KEY},
            params={"movieFileIds": batch},
            timeout=30,
        )
        if rr.ok:
            for mf in rr.json():
                index[mf["path"]] = mf
    return index


def load_sonarr_index(progress, task) -> dict:
    r = requests.get(f"{SONARR_URL}/api/v3/series", headers={"X-Api-Key": SONARR_KEY}, timeout=30)
    r.raise_for_status()
    series_list = [s for s in r.json() if s.get("statistics", {}).get("episodeFileCount", 0) > 0]
    progress.update(task, total=len(series_list))

    index = {}
    for s in series_list:
        rr = requests.get(
            f"{SONARR_URL}/api/v3/episodefile",
            headers={"X-Api-Key": SONARR_KEY},
            params={"seriesId": s["id"]},
            timeout=30,
        )
        if rr.ok:
            for ef in rr.json():
                index[ef["path"]] = ef
        progress.advance(task)
    return index


def enrich_with_arr(rec: FileRecord, radarr_idx: dict, sonarr_idx: dict):
    idx = radarr_idx if rec.group_kind == "movie" else sonarr_idx
    hit = None
    for variant in to_container_variants(rec.path):
        if variant in idx:
            hit = idx[variant]
            break
    if not hit:
        return
    rec.tracked_by = "Radarr" if rec.group_kind == "movie" else "Sonarr"
    rec.release_group = hit.get("releaseGroup") or ""
    rec.quality_name = (hit.get("quality") or {}).get("quality", {}).get("name", "")
    rec.custom_formats = [cf["name"] for cf in hit.get("customFormats", [])]
    rec.custom_format_score = hit.get("customFormatScore")
    rec.media_info = hit.get("mediaInfo") or {}


# ---------------------------------------------------------------------------
# qBittorrent active-seeding index (hit-and-run guard, same as dedupe-library.sh)
# ---------------------------------------------------------------------------

def load_qbit_active_paths() -> set:
    s = requests.Session()
    login = s.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=15)
    if login.status_code != 200 or login.text.strip() != "Ok.":
        console.print("[bold red]WARNING:[/bold red] could not log into qBittorrent — seeding-safety check is DISABLED for this run.")
        return set()
    r = s.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=30)
    r.raise_for_status()
    paths = set()
    for t in r.json():
        cp = t.get("content_path", "")
        paths.add(to_host_path(cp))
    return paths


def is_actively_seeding(path: str, qbit_paths: set) -> bool:
    for p in qbit_paths:
        if path == p or path.startswith(p + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Filename fallback parsing (only used for loose/untracked files)
# ---------------------------------------------------------------------------

HDR_KEYWORDS = re.compile(r"\b(DV|DoVi|Dolby[.\s]?Vision|HDR10\+|HDR10Plus|HDR)\b", re.I)
SOURCE_KEYWORDS = re.compile(r"\b(REMUX|BluRay|WEB-?DL|WEBRip|HDTV|DVD)\b", re.I)


def parse_filename_group(path: str) -> str:
    name = Path(path).stem
    m = re.search(r"[-.]([A-Za-z0-9]+)$", name)
    return m.group(1) if m else ""


def parse_filename_source(path: str) -> str:
    m = SOURCE_KEYWORDS.search(path)
    return m.group(1) if m else ""


def parse_filename_hdr(path: str) -> str:
    hits = set(h.upper() for h in HDR_KEYWORDS.findall(path))
    return "/".join(sorted(hits)) if hits else ""


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------

def fmt_size(n: int) -> str:
    return f"{n / (1024**3):.2f} GB"


def fmt_resolution(rec: FileRecord) -> str:
    if rec.media_info.get("resolution"):
        _w, h = rec.media_info["resolution"].split("x")
        h = int(h)
    else:
        h = rec.height
    if h >= 2000:
        return "2160p (4K)"
    if h >= 1000:
        return "1080p"
    if h >= 700:
        return "720p"
    return f"{h}p" if h else "?"


def fmt_hdr(rec: FileRecord) -> str:
    dyn = rec.media_info.get("videoDynamicRangeType")
    if dyn:
        return dyn
    if rec.dovi_present:
        return f"Dolby Vision (P{rec.dovi_profile})"
    if rec.color_trc == "smpte2084":
        return "HDR10"
    if rec.color_trc in ("arib-std-b67", "hlg"):
        return "HLG"
    fallback = parse_filename_hdr(rec.path)
    return fallback if fallback else "SDR"


def fmt_audio(rec: FileRecord) -> str:
    if rec.media_info.get("audioCodec"):
        ch = rec.media_info.get("audioChannels")
        return f"{rec.media_info['audioCodec']} {ch}ch" if ch else rec.media_info["audioCodec"]
    primary = None
    for codec, channels, _bitrate, extra in rec.audio_streams:
        if extra.get("ma:comment") == "1":
            continue
        primary = (codec or "", channels, extra)
        break
    if not primary:
        return f"{rec.plex_audio_codec} {rec.plex_audio_channels}ch"
    codec, channels, extra = primary
    label: str = {"dca": "DTS", "ac3": "AC3", "eac3": "E-AC3", "truehd": "TrueHD", "aac": "AAC"}.get(codec, codec)
    if extra.get("ma:profile") == "ma":
        label = "DTS-HD MA"
    return f"{label} {channels}ch" if channels else label


def fmt_video_codec(rec: FileRecord) -> str:
    if rec.media_info.get("videoCodec"):
        return rec.media_info["videoCodec"]
    return {"hevc": "HEVC (x265)", "h264": "AVC (x264)"}.get(rec.video_codec, rec.video_codec or "?")


def fmt_bitrate(rec: FileRecord) -> str:
    kbps = rec.media_info.get("videoBitrate")
    if kbps:
        return f"{kbps / 1000:.1f} Mbps"
    if rec.bitrate:
        return f"{rec.bitrate / 1000:.1f} Mbps"
    return "?"


def fmt_release_group(rec: FileRecord) -> str:
    return rec.release_group or parse_filename_group(rec.path) or "?"


def fmt_source(rec: FileRecord) -> str:
    return rec.quality_name or parse_filename_source(rec.path) or "?"


def fmt_score(rec: FileRecord) -> str:
    if rec.custom_format_score is None:
        return "[dim]n/a (loose)[/dim]"
    color = "green" if rec.custom_format_score >= 0 else "red"
    return f"[{color}]{rec.custom_format_score}[/{color}]"


def fmt_runtime(rec: FileRecord) -> str:
    if not rec.duration_ms:
        return "?"
    total_seconds = rec.duration_ms // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Suggestion heuristic — advisory only, never auto-applied
# ---------------------------------------------------------------------------

def suggest_index(recs: list[FileRecord]) -> Optional[int]:
    if durations_mismatch(recs):
        return None

    def key(rec: FileRecord):
        score = rec.custom_format_score if rec.custom_format_score is not None else -999999
        height = rec.height or int(rec.media_info.get("resolution", "0x0").split("x")[-1] or 0)
        hdr_rank = 2 if ("dolby vision" in fmt_hdr(rec).lower() or rec.dovi_present) else \
                   1 if fmt_hdr(rec) not in ("SDR", "") else 0
        return (score, height, hdr_rank, rec.size)
    best = max(range(len(recs)), key=lambda i: key(recs[i]))
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_groups(radarr_idx, sonarr_idx, qbit_paths, progress, task):
    records = load_plex_duplicate_groups(progress, task)
    groups: dict[str, list[FileRecord]] = {}
    for rec in records:
        enrich_with_arr(rec, radarr_idx, sonarr_idx)
        rec.actively_seeding = is_actively_seeding(rec.path, qbit_paths)
        groups.setdefault(rec.group_key, []).append(rec)
    return groups


def durations_mismatch(recs: list[FileRecord]) -> bool:
    durations = [r.duration_ms for r in recs if r.duration_ms]
    if len(durations) < 2:
        return False
    lo, hi = min(durations), max(durations)
    if hi == 0:
        return False
    return (hi - lo) > 30_000 and (hi - lo) / hi > 0.10


def render_group(console: Console, title: str, recs: list[FileRecord], suggested: Optional[int]):
    same_size = len({r.size for r in recs}) == 1
    mismatch = durations_mismatch(recs)
    header = f"[bold]{title}[/bold]"
    if mismatch:
        header += "\n[bold red]⚠ RUNTIMES DON'T MATCH — these are probably NOT the same content (Plex likely mis-grouped different files under one title). Verify manually before deleting anything.[/bold red]"
    elif same_size:
        header += "  [red]⚠ all files are the exact same size — likely a leftover exact duplicate, not a real quality choice[/red]"
    style = "red" if mismatch else "cyan"
    console.print(Panel(header, box=box.HEAVY, style=style))

    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("#")
    table.add_column("Runtime")
    table.add_column("Res")
    table.add_column("Source")
    table.add_column("Video")
    table.add_column("Bitrate")
    table.add_column("HDR/DV")
    table.add_column("Audio")
    table.add_column("Size")
    table.add_column("Group")
    table.add_column("Tracked")
    table.add_column("Score")
    table.add_column("Seeding")

    for i, rec in enumerate(recs, start=1):
        marker = "★ " if (suggested is not None and (i - 1) == suggested) else "  "
        row_style = "bold green" if (suggested is not None and (i - 1) == suggested) else None
        table.add_row(
            f"{marker}{i}",
            fmt_runtime(rec),
            fmt_resolution(rec),
            fmt_source(rec),
            fmt_video_codec(rec),
            fmt_bitrate(rec),
            fmt_hdr(rec),
            fmt_audio(rec),
            fmt_size(rec.size),
            fmt_release_group(rec),
            rec.tracked_by,
            fmt_score(rec),
            "[bold red]ACTIVE[/bold red]" if rec.actively_seeding else "",
            style=row_style,
        )
    console.print(table)
    for i, rec in enumerate(recs, start=1):
        console.print(f"  [{i}] [dim]{rec.path}[/dim]")
    if suggested is not None:
        console.print(f"  [dim]★ suggested keep (based on Radarr/Sonarr score, then resolution, then HDR/DV, then size — advisory only)[/dim]\n")
    else:
        console.print("  [dim]No suggestion offered — runtimes differ too much to treat these as comparable quality tiers.[/dim]\n")


def log(msg: str):
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    with open(LOGFILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(done: set):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(done), f)


def main():
    report_only = "--report-only" in sys.argv
    reset = "--reset" in sys.argv

    console.print(Panel("[bold]Plex Library Duplicate Review[/bold]\nFuzzy/quality duplicates — not exact-match dupes (those are handled by dedupe-library.sh)", style="magenta"))

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TimeElapsedColumn(), console=console,
    ) as progress:
        t1 = progress.add_task("Scanning Plex library...", total=None)
        radarr_idx = load_radarr_index()
        sonarr_idx = load_sonarr_index(progress, progress.add_task("Indexing Sonarr episode files...", total=None))
        qbit_paths = load_qbit_active_paths()
        groups = build_groups(radarr_idx, sonarr_idx, qbit_paths, progress, t1)

    console.print(f"\n[bold]{len(groups)}[/bold] titles have more than one version in your library.\n")

    done = set() if reset else load_state()
    total_freed = 0
    total_deleted = 0
    kept_all = 0

    movie_groups = {k: v for k, v in groups.items() if k.startswith("movie:")}
    ep_groups = {k: v for k, v in groups.items() if k.startswith("episode:")}
    console.print(f"  Movies: {len(movie_groups)}   TV episodes: {len(ep_groups)}")
    if done:
        console.print(f"  [dim]{len(done)} already reviewed in a previous session (resuming) — pass --reset to start over[/dim]")
    console.print()

    ordered_keys = sorted(groups.keys(), key=lambda k: groups[k][0].group_title)

    for key in ordered_keys:
        if key in done:
            continue
        recs = groups[key]
        suggested = suggest_index(recs)
        render_group(console, recs[0].group_title, recs, suggested)

        if report_only:
            done.add(key)
            continue

        choice = Prompt.ask(
            f"Keep which? (1-{len(recs)}, comma-separated for several, 'b'=keep both/all, 's'=skip for now, 'q'=quit)",
            default="s",
        )
        if choice.lower() == "q":
            save_state(done)
            break
        if choice.lower() in ("b", "s"):
            done.add(key)
            kept_all += 1
            save_state(done)
            console.print()
            continue

        try:
            keep_indices = {int(x.strip()) - 1 for x in choice.split(",") if x.strip()}
        except ValueError:
            console.print("[red]Couldn't parse that — skipping this group for now.[/red]\n")
            continue

        to_delete = [r for i, r in enumerate(recs) if i not in keep_indices]
        for rec in to_delete:
            if rec.actively_seeding:
                console.print(f"[bold red]⚠ {rec.path} is an ACTIVE qBittorrent torrent right now.[/bold red]")
                confirm = Prompt.ask("Deleting it will likely trigger a hit-and-run. Type DELETE to proceed anyway, or anything else to skip this file", default="")
                if confirm != "DELETE":
                    console.print("[yellow]Skipped.[/yellow]")
                    continue
            try:
                os.remove(rec.path)
                log(f"DELETED: {rec.path} (kept: {[recs[i].path for i in keep_indices]})")
                console.print(f"[green]Deleted:[/green] {rec.path}")
                total_deleted += 1
                total_freed += rec.size
            except OSError as e:
                console.print(f"[red]Failed to delete {rec.path}: {e}[/red]")
                log(f"FAILED to delete {rec.path}: {e}")

        done.add(key)
        save_state(done)
        console.print()

    console.print(Panel(
        f"Deleted: {total_deleted} files, freed {fmt_size(total_freed)}\n"
        f"Kept-both/skipped: {kept_all}\n\n"
        f"[bold]Remember:[/bold] run a Plex library scan + Empty Trash to stop these from showing as duplicate versions.",
        title="Session summary", style="cyan",
    ))


if __name__ == "__main__":
    main()
