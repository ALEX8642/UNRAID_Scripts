#!/usr/bin/env python3
"""
dupe-review.py

Interactive terminal tool for reviewing "fuzzy" duplicates in a Plex library — titles that
exist as more than one file (different quality/source/encode), as opposed to exact
byte-identical duplicates (see cross-disk-dedupe.sh for the one remaining class of those:
the same file physically present on two different array disks).

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

Safety model (see safe_ops.py): DRY RUN by default - your choices are written to a plan file and nothing
changes. `--apply` runs each removal after a y/N prompt; `--execute PLAN.json [--apply]` re-validates a saved
plan against the current state and runs it. Removals never go through /mnt/user: library-only files are
unlinked on the physical disk, seeded files go through qBittorrent's API, and a seed that may still be
inside a tracker's hit-and-run window is only tagged `delete-later` (its Plex-visible hardlink name is
removed, the seed keeps seeding). Hardlinked names of the same data are flagged in the table.
"""

import hashlib
import json
import os
import re
import shutil
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

from common import PATH_MAP, to_host_path
import safe_ops
from safe_ops import TorrentInfo, classify_removal, raw_copies

# ==== CONFIG — non-secret settings, edit directly for your setup ====
PLEX_DB = (
    "/plexdata/Plex Media Server/Plug-in Support/Databases/"
    "com.plexapp.plugins.library.db"
)

# Plex library_sections.id for the Movies/TV libraries Radarr/Sonarr manage — i.e. the ones
# PATH_MAP actually covers. Plex's "movie" metadata_type also covers other section types (Home
# Videos, Music Videos, Demo discs, etc.) that live under different host mounts PATH_MAP knows
# nothing about; without this filter those show up as "duplicates" too and can't be deleted.
# Check yours with: SELECT id, name, section_type FROM library_sections;
PLEX_LIBRARY_SECTION_IDS = [1, 2]

LOGFILE = "/logs/dupe-review.log"
# Dry-run is the default: choices are recorded in a plan file and nothing is changed. --apply executes immediately;
# --execute PLAN.json --apply re-validates and runs a saved plan. A separate state file keeps dry-run answers apart.
APPLY = "--apply" in sys.argv
STATE_FILE = "/logs/dupe-review-state.json" if APPLY else "/logs/dupe-review-plan-state.json"
PLAN_DIR = "/logs"

# Only require an extra confirmation before deleting an actively-seeding file if it's younger
# than this. Anything older is well past any realistic hit-and-run window, so it deletes with
# no extra prompt.
PROTECT_AGE_DAYS = 30

# Where soft-deleted files go when run with --trash instead of being removed outright. Not
# auto-cleaned — that's a deliberate separate step, run your own cleanup on a schedule you trust.
TRASH_DIR = "/logs/trash"
# ==== END CONFIG ====

# ==== CONFIG — credentials/connection info: set via a .env file (see .env.example), never
# hardcode real values here — this file is committed to a public repo ====
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
# Optional: Plex Settings -> Account, or find PlexOnlineToken in Preferences.xml. If unset, the
# tool just reminds you to scan + empty trash yourself instead of doing it automatically.
PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
# ==== END CONFIG ====

console = Console()


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
    path_mapped: bool = True  # False if no PATH_MAP prefix matched — deletion refused
    season_key: str = ""  # "<show>::S<NN>" for episodes, "" for movies — used to batch a season
    torrent_hash: str = ""  # qBittorrent hash this file belongs to, if any
    arr_id: int = 0  # Radarr movieId / Sonarr seriesId, if tracked_by is Radarr/Sonarr
    raw: list = field(default_factory=list)   # physical /mnt/diskN copies of this path
    nlink: int = 0                             # hardlink count (0 = unknown)
    ident: tuple = ()                          # (device, inode) of the physical file
    same_as: int = 0                           # 1-based index of an earlier version that is the SAME data (hardlink)


def open_plex_db():
    uri = f"file:{PLEX_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def validate_library_sections(cur) -> None:
    cur.execute("SELECT id, name, section_type FROM library_sections")
    all_sections = cur.fetchall()
    valid_ids = {row["id"] for row in all_sections}
    missing = [i for i in PLEX_LIBRARY_SECTION_IDS if i not in valid_ids]
    if not missing:
        return
    console.print(f"[bold red]WARNING:[/bold red] PLEX_LIBRARY_SECTION_IDS {missing} don't exist in this Plex database — every query will silently return 0 rows for them.")
    console.print("Available library sections:")
    for row in all_sections:
        console.print(f"  id={row['id']}  {row['name']!r}  (section_type={row['section_type']})")
    console.print("Edit PLEX_LIBRARY_SECTION_IDS in the CONFIG block to match your Movies/TV library IDs.\n")


def load_plex_duplicate_groups(progress, task) -> list[FileRecord]:
    con = open_plex_db()
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    validate_library_sections(cur)

    section_placeholders = ",".join("?" * len(PLEX_LIBRARY_SECTION_IDS))
    cur.execute(
        f"""
        SELECT m.metadata_type, m.id AS metadata_item_id, m.title, m.year,
               m."index" AS ep_index, m.parent_id,
               mi.id AS media_item_id, mi.width, mi.height, mi.bitrate, mi.duration,
               mi.video_codec, mi.audio_codec, mi.audio_channels, mi.container, mi.color_trc,
               mp.file, mp.size
        FROM metadata_items m
        JOIN media_items mi ON mi.metadata_item_id = m.id AND mi.deleted_at IS NULL
        JOIN media_parts mp ON mp.media_item_id = mi.id
        WHERE m.metadata_type IN (1, 4)
          AND mi.library_section_id IN ({section_placeholders})
          AND m.id IN (
            SELECT metadata_item_id FROM media_items
            WHERE deleted_at IS NULL
              AND library_section_id IN ({section_placeholders})
            GROUP BY metadata_item_id HAVING COUNT(*) > 1
          )
        ORDER BY m.metadata_type, m.id
        """,
        PLEX_LIBRARY_SECTION_IDS + PLEX_LIBRARY_SECTION_IDS,
    )
    rows = cur.fetchall()
    progress.update(task, total=len(rows) + 2)

    # Episode parent hierarchy (show/season titles) for episode groups
    ep_ids = sorted({r["metadata_item_id"] for r in rows if r["metadata_type"] == 4})
    ep_titles = {}
    ep_season_keys = {}
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
            ep_season_keys[r["id"]] = f"{r['show_title']}::S{season_num:02d}"
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

        # Plex stores paths using its own container convention (e.g. "/tv/Show.mkv"). Convert
        # to the real host path immediately — this is the path actually used for deletion,
        # mtime checks, and display, so there is exactly one source of truth rather than a raw
        # Plex path that quietly never gets translated before os.remove().
        host_path, path_mapped = to_host_path(r["file"])

        rec = FileRecord(
            group_key=group_key,
            group_title=group_title,
            group_kind=kind,
            media_item_id=r["media_item_id"],
            metadata_item_id=r["metadata_item_id"],
            path=host_path,
            path_mapped=path_mapped,
            season_key=ep_season_keys.get(r["metadata_item_id"], ""),
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
    rec.arr_id = hit.get("movieId") or hit.get("seriesId") or 0


# ---------------------------------------------------------------------------
# qBittorrent active-seeding index (hit-and-run guard)
# ---------------------------------------------------------------------------

QBIT_SESSION = None
QBIT_TORRENTS: dict = {}
QBIT_INDEX: dict = {}
QBIT_SKIPPED = 0     # torrents whose location could not be indexed; while this is non-zero the seeding check is incomplete
# qBittorrent mounts that can never hold library files (music, adult, software...). Torrents there are irrelevant, not "unindexable".
NON_LIBRARY_ROOTS = tuple(r for r in os.environ.get("NON_LIBRARY_ROOTS", "/music,/musicvideos,/stash,/VR,/software,/downloads/incomplete,/run").split(",") if r)


def _under(path: str, roots) -> bool:
    """Path-boundary match: '/run' covers '/run/x' but not '/runway-movies/x'."""
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def load_qbit_index() -> dict:
    """host path (file or folder) -> torrent hash, for every torrent's content_path."""
    global QBIT_SESSION, QBIT_TORRENTS, QBIT_SKIPPED
    QBIT_SKIPPED = 0
    s = requests.Session()
    login = s.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=15)
    if login.status_code != 200 or login.text.strip() != "Ok.":
        console.print("[bold red]WARNING:[/bold red] could not log into qBittorrent — seeding-safety check is DISABLED for this run.")
        return {}
    r = s.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=30)
    r.raise_for_status()
    QBIT_SESSION = s
    QBIT_TORRENTS = {t["hash"]: t for t in r.json()}
    degenerate = {prefix.rstrip("/") for prefix, _ in PATH_MAP}
    index = {}
    for t in r.json():
        cp = (t.get("content_path", "") or "").rstrip("/")
        h = t.get("hash", "")
        if not cp:
            continue
        if cp not in degenerate:
            host_path, mapped = to_host_path(cp)
            if mapped:
                # Same caution as FileRecord.path_mapped: an unrecognized mount is not
                # something to guess about, so don't let it participate in the match.
                # A LIST of hashes: the same release cross-seeded to two trackers must not collapse to whichever came last.
                index.setdefault(host_path, []).append(h)
            elif not _under(cp, NON_LIBRARY_ROOTS):
                QBIT_SKIPPED += 1      # an unknown mount that is not a known non-library root: its files would look unseeded
            continue
        # content_path is exactly a bare category root ("/tv", "/movies", ...). This could be
        # a genuinely broken/placeholder torrent entry with no real content (the original
        # reason for this check — without it, to_host_path() would leave it unconverted and
        # the prefix match below would then match every file in the library as actively
        # seeding), OR it could be a legitimate multi-file torrent with "Keep top level
        # folder" disabled in qBittorrent, whose real files sit directly under that root.
        # content_path alone can't distinguish those, and skipping unconditionally would
        # silently strip hit-and-run protection from every file in a torrent of the second
        # kind. Fetch its actual file list instead and index each file individually — a
        # precise per-file path can't cause the "matches everything" problem the way the bare
        # folder string would.
        save_path, mapped = to_host_path((t.get("save_path", "") or "").rstrip("/"))
        if not mapped:
            if not _under(save_path, NON_LIBRARY_ROOTS):
                QBIT_SKIPPED += 1
            continue
        rr = s.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": h}, timeout=30)
        if not rr.ok:
            QBIT_SKIPPED += 1
            continue
        for f in rr.json():
            index.setdefault(os.path.join(save_path, f["name"]), []).append(h)
    QBIT_INDEX.update(index)
    return index


def torrent_hashes_for(path: str, qbit_index: dict) -> list:
    """Every torrent whose tracked file/folder is `path` or contains it (cross-seeds included)."""
    out = []
    for p, hs in qbit_index.items():
        if path == p or path.startswith(p + "/"):
            out += [h for h in (hs if isinstance(hs, list) else [hs]) if h not in out]
    return out


def torrent_hash_for(path: str, qbit_index: dict) -> Optional[str]:
    hs = torrent_hashes_for(path, qbit_index)
    return hs[0] if hs else None


def file_age_days(path: str) -> int:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0
    return int((time.time() - mtime) / 86400)


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


def fmt_seeding(rec: FileRecord) -> str:
    if not rec.actively_seeding:
        return ""
    age = file_age_days(rec.path)
    if age < PROTECT_AGE_DAYS:
        return f"[bold red]ACTIVE ({age}d)[/bold red]"
    return f"[dim]active, {age}d — past HNR window[/dim]"


def fmt_runtime(rec: FileRecord) -> str:
    if not rec.duration_ms:
        return "?"
    total_seconds = rec.duration_ms // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Per-cell "notably better" highlights — separate from the overall ★ suggestion.
# Never applied when a 4K Remux is in the group (especially from FraMeSToR): a top-tier
# remux's audio is already about as good as it gets, and DV-profile device compatibility is a
# narrower concern than remux fidelity, so another file never gets flagged "better" on these
# specific axes just because it edges out the remux on codec/profile alone.
# ---------------------------------------------------------------------------

AUDIO_TIER = {
    "truehd": 3, "dts-hd ma": 3, "flac": 3, "pcm": 3, "dts-hd": 3,
    "eac3": 1, "e-ac3": 1, "ddp": 1, "dts": 1,
    "ac3": 0, "dd": 0, "aac": 0,
}

# Rough device-compatibility ordering, not a quality ordering: profile 8 (single-layer,
# HDR10-compatible base) plays broadly; profile 7 (dual-layer) needs client-side conversion on
# most non-DV-native devices; profile 5 (Apple-only, no fallback) is the narrowest.
DV_PROFILE_COMPAT = {"8": 2, "7": 1, "5": 0}


def is_premium_remux(rec: FileRecord) -> bool:
    height = rec.height or int(rec.media_info.get("resolution", "0x0").split("x")[-1] or 0)
    return height >= 2000 and "remux" in fmt_source(rec).lower()


def audio_rank(rec: FileRecord) -> tuple:
    label = fmt_audio(rec).lower()
    tier = 0
    for key, t in AUDIO_TIER.items():
        if key in label:
            tier = max(tier, t)
    channels = rec.media_info.get("audioChannels") or rec.plex_audio_channels or 0
    try:
        channels = float(channels)
    except (TypeError, ValueError):
        channels = 0
    return (tier, channels)


def dv_profile_compat(rec: FileRecord) -> Optional[int]:
    return DV_PROFILE_COMPAT.get(rec.dovi_profile)


def compute_highlights(recs: list[FileRecord]) -> dict:
    """index -> {'audio': bool, 'dv': bool} — cells to render in green."""
    highlights = {i: {"audio": False, "dv": False} for i in range(len(recs))}
    if any(is_premium_remux(r) for r in recs):
        return highlights

    audio_ranks = [audio_rank(r) for r in recs]
    if len(set(audio_ranks)) > 1:
        best = max(range(len(recs)), key=lambda i: audio_ranks[i])
        highlights[best]["audio"] = True

    dv_ranks: list[int] = [r for r in (dv_profile_compat(rec) for rec in recs) if r is not None]
    comparable = [i for i, r in enumerate(recs) if dv_profile_compat(r) is not None]
    if len(comparable) >= 2 and len(set(dv_ranks)) > 1:
        best = max(comparable, key=lambda i: dv_profile_compat(recs[i]) or -1)
        highlights[best]["dv"] = True

    return highlights


# ---------------------------------------------------------------------------
# Suggestion heuristic — advisory only, never auto-applied
# ---------------------------------------------------------------------------

def suggest_index(recs: list[FileRecord]) -> Optional[int]:
    if durations_mismatch(recs) or multi_part_mismatch(recs):
        return None

    def key(rec: FileRecord):
        # Resolution and HDR/DV are objective and always known; the Radarr/Sonarr score is only
        # known for whichever file that app happens to track. An untracked file with no score
        # is not a *worse* file — it's an unscored one — so score must not outrank resolution/
        # HDR or it'll pick a tracked SDR copy over an untracked Dolby Vision one of the same
        # or higher resolution. Score only breaks ties between files already equal on those.
        height = rec.height or int(rec.media_info.get("resolution", "0x0").split("x")[-1] or 0)
        hdr_rank = 2 if ("dolby vision" in fmt_hdr(rec).lower() or rec.dovi_present) else \
                   1 if fmt_hdr(rec) not in ("SDR", "") else 0
        score = rec.custom_format_score if rec.custom_format_score is not None else 0
        return (height, hdr_rank, score, rec.size)
    best = max(range(len(recs)), key=lambda i: key(recs[i]))
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_groups(radarr_idx, sonarr_idx, qbit_index, progress, task):
    records = load_plex_duplicate_groups(progress, task)
    groups: dict[str, list[FileRecord]] = {}
    for rec in records:
        enrich_with_arr(rec, radarr_idx, sonarr_idx)
        h = torrent_hash_for(rec.path, qbit_index)
        rec.actively_seeding = h is not None
        rec.torrent_hash = h or ""
        rec.raw = raw_copies(rec.path)
        if len(rec.raw) == 1 and os.path.exists(rec.raw[0]):
            dev, ino, nl = safe_ops.file_identity(rec.raw[0])
            rec.ident, rec.nlink = (dev, ino), nl
        groups.setdefault(rec.group_key, []).append(rec)
    for recs in groups.values():          # versions that are literally the same data under two names
        seen = {}
        for i, rec in enumerate(recs, start=1):
            if rec.ident:
                if rec.ident in seen:
                    rec.same_as = seen[rec.ident]
                else:
                    seen[rec.ident] = i
    return groups


def durations_mismatch(recs: list[FileRecord]) -> bool:
    durations = [r.duration_ms for r in recs if r.duration_ms]
    if len(durations) < 2:
        return False
    lo, hi = min(durations), max(durations)
    if hi == 0:
        return False
    return (hi - lo) > 30_000 and (hi - lo) / hi > 0.10


PART_PATTERN = re.compile(r"(?:disc|disk|part|cd)[\s._-]*([0-9]+)", re.IGNORECASE)


def part_number(rec: FileRecord) -> Optional[int]:
    m = PART_PATTERN.search(Path(rec.path).name)
    return int(m.group(1)) if m else None


def multi_part_mismatch(recs: list[FileRecord]) -> bool:
    """True if filenames indicate different parts/discs of one release (e.g. Disc 1 vs Disc 2)
    rather than alternate versions of the same content — Plex can group these under one title
    since they share a title/year match despite being genuinely different content."""
    parts = {part_number(r) for r in recs}
    parts.discard(None)
    return len(parts) > 1


def render_group(console: Console, title: str, recs: list[FileRecord], suggested: Optional[int], position_label: str = ""):
    same_size = len({r.size for r in recs}) == 1
    mismatch = durations_mismatch(recs)
    multi_part = multi_part_mismatch(recs)
    header = f"[bold]{title}[/bold]"
    if position_label:
        header = f"[dim]{position_label}[/dim]  " + header
    if mismatch:
        header += "\n[bold red]⚠ RUNTIMES DON'T MATCH — these are probably NOT the same content (Plex likely mis-grouped different files under one title). Verify manually before deleting anything.[/bold red]"
    elif multi_part:
        header += "\n[bold red]⚠ LOOKS LIKE DIFFERENT DISCS/PARTS OF ONE RELEASE (e.g. Disc 1 vs Disc 2) — NOT alternate versions of the same content. Verify manually before deleting anything.[/bold red]"
    elif same_size:
        header += "  [red]⚠ all files are the exact same size — likely a leftover exact duplicate, not a real quality choice[/red]"
    style = "red" if (mismatch or multi_part) else "cyan"
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

    highlights = compute_highlights(recs)
    for i, rec in enumerate(recs, start=1):
        marker = "★ " if (suggested is not None and (i - 1) == suggested) else "  "
        row_style = "bold green" if (suggested is not None and (i - 1) == suggested) else None
        hdr_text = fmt_hdr(rec)
        if highlights[i - 1]["dv"]:
            hdr_text = f"[bold green]{hdr_text}[/bold green]"
        audio_text = fmt_audio(rec)
        if highlights[i - 1]["audio"]:
            audio_text = f"[bold green]{audio_text}[/bold green]"
        table.add_row(
            f"{marker}{i}",
            fmt_runtime(rec),
            fmt_resolution(rec),
            fmt_source(rec),
            fmt_video_codec(rec),
            fmt_bitrate(rec),
            hdr_text,
            audio_text,
            fmt_size(rec.size),
            fmt_release_group(rec),
            rec.tracked_by,
            fmt_score(rec),
            fmt_seeding(rec),
            style=row_style,
        )
    console.print(table)
    for i, rec in enumerate(recs, start=1):
        unmapped_note = "  [bold red]⚠ unrecognized mount — cannot be deleted by this tool[/bold red]" if not rec.path_mapped else ""
        link_note = f"  [yellow]⇄ same data as [{rec.same_as}] (hardlink) - deleting one name frees no space[/yellow]" if rec.same_as else (f"  [dim](hardlinked x{rec.nlink})[/dim]" if rec.nlink > 1 else "")
        console.print(f"  [{i}] [dim]{rec.path}[/dim]{unmapped_note}{link_note}")
    if suggested is not None:
        console.print(f"  [dim]★ suggested keep (based on Radarr/Sonarr score, then resolution, then HDR/DV, then size — advisory only)[/dim]")
    else:
        why = "runtimes differ too much" if mismatch else "filenames look like different discs/parts" if multi_part else "not comparable"
        console.print(f"  [dim]No suggestion offered — {why} to treat these as the same content.[/dim]")
    if any(h["audio"] or h["dv"] for h in highlights.values()):
        console.print("  [dim][bold green]green[/bold green] Audio/HDR-DV cell = notably better audio codec or a more broadly-supported DV profile (suppressed when a 4K Remux is in the group)[/dim]")
    console.print()


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


def group_fingerprint(recs: list[FileRecord]) -> str:
    sig = "|".join(sorted(f"{r.path}:{r.size}" for r in recs))
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def state_key(key: str, recs: list[FileRecord]) -> str:
    # Tied to the specific file set (path+size), not just the title — if a new duplicate shows
    # up for a title already marked reviewed (a re-grab, a manual add), the fingerprint changes
    # and it resurfaces instead of staying silently marked done forever.
    return f"{key}::{group_fingerprint(recs)}"


def reconcile_arr(kept: FileRecord, deleted_tracked: FileRecord) -> None:
    """After removing the file Radarr/Sonarr was tracking: point the app at the file that was kept and
    rescan, so it tracks a real file again. Never searches or grabs. Dry-run unless --apply.
    Radarr: movie path -> the kept file's folder (PUT ?moveFiles=false) then RescanMovie.
    Sonarr: RescanSeries (the kept file must live under the series folder to be picked up)."""
    if not deleted_tracked.arr_id:
        return
    movie = deleted_tracked.group_kind == "movie"
    base, key = (RADARR_URL, RADARR_KEY) if movie else (SONARR_URL, SONARR_KEY)
    hdr = {"X-Api-Key": key}
    app = deleted_tracked.tracked_by
    try:
        if movie:
            m = requests.get(f"{base}/api/v3/movie/{deleted_tracked.arr_id}", headers=hdr, timeout=30).json()
            kept_dir = os.path.dirname(kept.path)
            rel = kept_dir.split(safe_ops.USER_PREFIX + "Media/", 1)[1] if (safe_ops.USER_PREFIX + "Media/") in kept_dir else ""
            new_path = "/media/" + rel
            if rel in ("", "Movies") or not rel.startswith(("Movies/", "tv/")):
                console.print(f"    [yellow]{app}: the kept file is a bare file at the library root - move it into a Title (Year) folder first, then rescan. Not changed.[/yellow]")
                return
            if m.get("path") != new_path:
                console.print(f"    [cyan]{app}: {'repoint' if APPLY else 'WOULD repoint'} movie path {m.get('path')} -> {new_path}[/cyan]")
                if APPLY:
                    m["path"] = new_path
                    requests.put(f"{base}/api/v3/movie/{deleted_tracked.arr_id}", headers=hdr, params={"moveFiles": "false"}, json=m, timeout=60).raise_for_status()
            if APPLY:
                requests.post(f"{base}/api/v3/command", headers=hdr, json={"name": "RescanMovie", "movieIds": [deleted_tracked.arr_id]}, timeout=30).raise_for_status()
        else:
            console.print(f"    [cyan]{app}: {'rescan' if APPLY else 'WOULD rescan'} series {deleted_tracked.arr_id} (kept file must be under the series folder)[/cyan]")
            if APPLY:
                requests.post(f"{base}/api/v3/command", headers=hdr, json={"name": "RescanSeries", "seriesId": deleted_tracked.arr_id}, timeout=30).raise_for_status()
        log(f"RECONCILE {'APPLIED' if APPLY else 'PLANNED'}: {app} id={deleted_tracked.arr_id} kept={kept.path}")
    except (requests.RequestException, KeyError, ValueError) as e:
        console.print(f"    [yellow]{app} reconcile failed: {e}. Check manually.[/yellow]")
        log(f"RECONCILE FAILED {app} id={deleted_tracked.arr_id}: {e}")


def plex_refresh_and_empty_trash() -> None:
    if not PLEX_TOKEN:
        console.print("[dim]PLEX_TOKEN not set — skipping automatic Plex scan/empty-trash. Run a library scan + Empty Trash yourself.[/dim]")
        return
    console.print("[dim]Triggering Plex library refresh for Movies/TV...[/dim]")
    ok = True
    for section_id in PLEX_LIBRARY_SECTION_IDS:
        try:
            r = requests.get(f"{PLEX_URL}/library/sections/{section_id}/refresh", params={"X-Plex-Token": PLEX_TOKEN}, timeout=15)
            if not r.ok:
                console.print(f"[red]Plex refresh for section {section_id} returned HTTP {r.status_code} — check PLEX_URL/PLEX_TOKEN.[/red]")
                ok = False
        except requests.RequestException as e:
            console.print(f"[red]Plex refresh failed for section {section_id}: {e}[/red]")
            ok = False
    console.print("[dim]Waiting for the scan before emptying trash (best-effort — rerun Empty Trash yourself if some duplicates linger)...[/dim]")
    time.sleep(20)
    for section_id in PLEX_LIBRARY_SECTION_IDS:
        try:
            r = requests.put(f"{PLEX_URL}/library/sections/{section_id}/emptyTrash", params={"X-Plex-Token": PLEX_TOKEN}, timeout=15)
            if not r.ok:
                console.print(f"[red]Plex empty-trash for section {section_id} returned HTTP {r.status_code}.[/red]")
                ok = False
        except requests.RequestException as e:
            console.print(f"[red]Plex empty-trash failed for section {section_id}: {e}[/red]")
            ok = False
    if ok:
        console.print("[green]Plex library refreshed and trash emptied for Movies/TV.[/green]")


PLAN: list = []


def qbit_n_files(h: str) -> int:
    try:
        r = QBIT_SESSION.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": h}, timeout=30)
        body = r.json()
        n = len(body) if r.ok and isinstance(body, list) else 0
        return n if n >= 1 else 999     # an empty/error answer is "unknown", never "zero files, so not a pack"
    except Exception:
        return 999          # unknown -> treated as a pack -> refused


def torrent_for(rec) -> Optional[TorrentInfo]:
    """The torrent that owns rec's data: either rec's own tracked name, or - for a hardlinked LIBRARY name -
    a seed kept under arr/ (found through the shared inode). With several torrents on the same data (cross-seeding) the
    YOUNGEST decides, and the others are counted so a delete that would break them is refused. None if no torrent claims it."""
    if QBIT_SESSION is None:
        return None
    hs = torrent_hashes_for(rec.path, QBIT_INDEX)
    exact = True
    if not hs and len(rec.raw) == 1 and rec.nlink > 1:
        for n in safe_ops.other_names(rec.raw[0]):
            if "/Media/" not in n:
                continue
            host = safe_ops.USER_PREFIX + "Media/" + n.split("/Media/", 1)[1]
            hs = torrent_hashes_for(host, QBIT_INDEX)
            if hs:
                exact = False
                break
    if not hs:
        return None
    def days(h):
        return (QBIT_TORRENTS.get(h, {}).get("seeding_time", 0) or 0) / 86400.0
    h = min(hs, key=days)
    t = QBIT_TORRENTS.get(h, {})
    return TorrentInfo(hash=h, name=t.get("name", h), seed_days=days(h), category=t.get("category", ""),
                       n_files=qbit_n_files(h), covers_exact_path=exact, shared_with=len(hs) - 1)


def hardlink_problem(rec) -> str:
    """A file with more names than we can account for is shared with something we cannot see (a seed outside arr/?). Refuse."""
    if getattr(rec, "nlink", 0) > 1 and len(getattr(rec, "raw", [])) == 1:
        found = safe_ops.other_names(rec.raw[0])
        if len(found) < rec.nlink - 1:
            return f"{rec.nlink} hardlinked names but only {len(found) + 1} found under arr/: another name (a seed?) exists elsewhere"
    return ""


def veto(dec, tinfo, keeper_recs, loser_hash=""):
    """Last-line checks no classification can see: the keeper must be a real file, and must not be owned by the torrent we
    are about to delete. Returns a REFUSE Decision or the original."""
    from safe_ops import Decision
    prob = safe_ops.keeper_problem([k.raw for k in keeper_recs])
    if prob:
        return Decision("REFUSE", prob)
    if dec.delete_torrent and tinfo:
        for k in keeper_recs:
            if tinfo.hash in torrent_hashes_for(k.path, QBIT_INDEX):
                return Decision("REFUSE", "the file being kept belongs to the same torrent that would be deleted", torrent_hash=tinfo.hash, torrent_name=tinfo.name)
    return dec


MAX_REMOVALS_PER_SESSION = int(os.environ.get("DUPE_MAX_REMOVALS", "50"))
_removals = {"n": 0}


def delete_one(rec: FileRecord, to_keep: list, use_trash: bool, run_stamp: str) -> str:
    """Decide how to remove `rec` (safe_ops.classify_removal), record it in the plan and - only with --apply, after a
    y/N confirmation - execute it. Returns 'planned' (dry run), 'deleted', 'tagged' (kept seeding + tagged), 'declined', 'error'."""
    if not rec.path_mapped:
        console.print(f"    [red]Refusing - path doesn't match any known host mount: {rec.path}[/red]")
        return "unmapped"
    keeper_raws = [k.raw[0] for k in to_keep if len(k.raw) == 1]
    kr = rec.raw[0] if rec.raw and rec.raw[0] in keeper_raws else ""
    tinfo = torrent_for(rec)
    dec = classify_removal(rec.raw, tinfo, keeper_raw=kr)
    if dec.mode != "REFUSE":
        hp = hardlink_problem(rec)
        dec = safe_ops.Decision("REFUSE", hp) if hp else veto(dec, tinfo, to_keep)
    seed_note = f" (torrent '{tinfo.name[:40]}', seeded {tinfo.seed_days:.1f}d, {'own name' if tinfo.covers_exact_path else 'seed kept under arr/'})" if tinfo else ""
    console.print(f"    [bold]{dec.mode}[/bold] - {dec.reason}{seed_note}")
    PLAN.append(dict(title=rec.group_title, kind=rec.group_kind, loser=rec.path, keep=[k.path for k in to_keep], mode=dec.mode,
                     torrent=dec.torrent_hash, arr=rec.tracked_by, arr_id=rec.arr_id, size=rec.size))
    if dec.mode == "REFUSE":
        log(f"REFUSED {rec.path}: {dec.reason}")
        return "error"
    if APPLY and _removals["n"] >= MAX_REMOVALS_PER_SESSION:
        console.print(f"    [yellow]Per-session limit of {MAX_REMOVALS_PER_SESSION} removals reached; stopping here. Start a new session to continue.[/yellow]")
        return "declined"
    if APPLY:
        ans = Prompt.ask("    Execute this removal? [y/N]", default="n").strip().lower()
        if ans != "y":
            console.print("    [yellow]Skipped.[/yellow]")
            return "declined"
    lines = safe_ops.execute(dec, QBIT_SESSION, QBIT_URL, apply=APPLY, logfile=LOGFILE, why=f"dupe-review keep={[k.path for k in to_keep]}")
    for line in lines:
        console.print(f"      {line}")
    if APPLY and not safe_ops.succeeded(lines):
        console.print("    [red]The removal did not complete; nothing is marked as reviewed and the arr is not changed.[/red]")
        return "error"
    removed = bool(dec.unlink_raw or dec.delete_torrent)
    if APPLY and removed:
        _removals["n"] += 1
    if not APPLY:
        return "planned" if removed else "tagged"
    return "deleted" if removed else "tagged"


def require_qbit_for_apply() -> None:
    """Fail closed: without a qBittorrent session torrent_for() returns None for everything, which would classify seeded
    files as free to unlink. Applying without the seeding check is never acceptable."""
    if APPLY and QBIT_SESSION is None:
        console.print("[bold red]Refusing to --apply: qBittorrent is unreachable, so seeded files cannot be recognised.[/bold red]")
        sys.exit(2)
    if APPLY and (not QBIT_TORRENTS or QBIT_SKIPPED):
        console.print(f"[bold red]Refusing to --apply: the qBittorrent index is incomplete ({len(QBIT_TORRENTS)} torrents, {QBIT_SKIPPED} could not be located). "
                      "Seeded files might look unseeded.[/bold red]")
        sys.exit(2)


def execute_plan(path: str) -> None:
    """Re-validate and run a plan written by a dry-run review. Every action is re-classified against the CURRENT
    state; anything that changed since planning is skipped, not guessed at. Dry-run unless --apply."""
    from types import SimpleNamespace
    actions = json.load(open(path))
    console.print(Panel(f"Plan {path}: {len(actions)} action(s) - {'APPLY' if APPLY else 'DRY RUN (add --apply to execute)'}", style="magenta"))
    load_qbit_index()
    require_qbit_for_apply()
    done = 0
    for a in actions:
        raw = raw_copies(a["loser"])
        nl = safe_ops.file_identity(raw[0])[2] if len(raw) == 1 and os.path.exists(raw[0]) else 0
        r = SimpleNamespace(path=a["loser"], raw=raw, nlink=nl)
        if not a["loser"].startswith((safe_ops.USER_PREFIX + "Media/Movies/", safe_ops.USER_PREFIX + "Media/tv/")) or os.path.normpath(a["loser"]) != a["loser"]:
            console.print(f"  [yellow]SKIP {a['title']}: the plan names a path outside the Movies/tv libraries[/yellow]")
            continue
        keeper_recs = [SimpleNamespace(path=k, raw=raw_copies(k)) for k in a["keep"]]
        keeper_raws = [k.raw[0] for k in keeper_recs if len(k.raw) == 1]
        if not keeper_raws or len(keeper_raws) != len(a["keep"]):
            console.print(f"  [yellow]SKIP {a['title']}: a kept file is missing now - not removing anything[/yellow]")
            continue
        tinfo = torrent_for(r)
        dec = classify_removal(raw, tinfo, keeper_raw=raw[0] if raw and raw[0] in keeper_raws else "")
        if dec.mode != "REFUSE":
            hp = hardlink_problem(r)
            dec = safe_ops.Decision("REFUSE", hp) if hp else veto(dec, tinfo, keeper_recs)
        if dec.mode != a["mode"] or (dec.torrent_hash or "") != (a.get("torrent") or ""):
            console.print(f"  [yellow]SKIP {a['title']}: state changed since planning (was {a['mode']}, now {dec.mode}: {dec.reason})[/yellow]")
            continue
        if a.get("size") and raw and os.path.exists(raw[0]) and os.path.getsize(raw[0]) != a["size"]:
            console.print(f"  [yellow]SKIP {a['title']}: the file's size changed since planning[/yellow]")
            continue
        console.print(f"  [bold]{a['title']}[/bold]: {dec.mode}")
        lines = safe_ops.execute(dec, QBIT_SESSION, QBIT_URL, apply=APPLY, logfile=LOGFILE, why=f"plan {os.path.basename(path)}")
        for line in lines:
            console.print(f"      {line}")
        if APPLY and not safe_ops.succeeded(lines):
            console.print("  [red]The removal did not complete: not reconciling Radarr/Sonarr and not counting it as done.[/red]")
            continue
        if a.get("arr") in ("Radarr", "Sonarr") and len(a["keep"]) == 1 and (dec.unlink_raw or dec.delete_torrent):
            reconcile_arr(SimpleNamespace(path=a["keep"][0]), SimpleNamespace(group_kind=a["kind"], arr_id=a["arr_id"], tracked_by=a["arr"]))
        done += 1
    if APPLY and done:
        plex_refresh_and_empty_trash()
    console.print(f"[bold]{done}[/bold] action(s) {'executed' if APPLY else 'validated (dry run)'}.")


# ---------------------------------------------------------------------------
# Season batching — when every episode in a season has one side sharing the same
# qBittorrent torrent (a season-pack grab vs the individually Sonarr-managed copies),
# review the whole season as one decision instead of once per episode.
# ---------------------------------------------------------------------------

def build_season_clusters(groups: dict) -> list[dict]:
    buckets: dict[tuple, list] = {}
    for key, recs in groups.items():
        if not key.startswith("episode:") or len(recs) != 2:
            continue
        season_key = recs[0].season_key
        if not season_key:
            continue
        hashes = {r.torrent_hash for r in recs if r.torrent_hash}
        if len(hashes) != 1:
            continue
        h = next(iter(hashes))
        side_a = [r for r in recs if r.torrent_hash == h]
        side_b = [r for r in recs if r.torrent_hash != h]
        if len(side_a) != 1 or len(side_b) != 1:
            continue
        buckets.setdefault((season_key, h), []).append((key, side_a[0], side_b[0]))

    clusters = []
    for (season_key, h), items in buckets.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[1].group_title)
        clusters.append({"season_key": season_key, "hash": h, "items": items})
    clusters.sort(key=lambda c: c["season_key"])
    return clusters


def render_season_cluster(cluster: dict, position_label: str = ""):
    season_key = cluster["season_key"]
    items = cluster["items"]
    show, _, season = season_key.partition("::")
    sample_a, sample_b = items[0][1], items[0][2]
    total_a = sum(a.size for _, a, _ in items)
    total_b = sum(b.size for _, _, b in items)

    header = f"[bold]{show} — {season}[/bold]  ({len(items)} episodes share one torrent on one side)"
    if position_label:
        header = f"[dim]{position_label}[/dim]  " + header
    console.print(Panel(header, box=box.HEAVY, style="cyan"))

    highlights = compute_highlights([sample_a, sample_b])
    hdr_a = fmt_hdr(sample_a)
    hdr_b = fmt_hdr(sample_b)
    audio_a = fmt_audio(sample_a)
    audio_b = fmt_audio(sample_b)
    if highlights[0]["dv"]:
        hdr_a = f"[bold green]{hdr_a}[/bold green]"
    if highlights[1]["dv"]:
        hdr_b = f"[bold green]{hdr_b}[/bold green]"
    if highlights[0]["audio"]:
        audio_a = f"[bold green]{audio_a}[/bold green]"
    if highlights[1]["audio"]:
        audio_b = f"[bold green]{audio_b}[/bold green]"

    table = Table(box=box.SIMPLE_HEAVY)
    for col in ("Side", "Res", "Source", "Video", "HDR/DV", "Audio", "Total Size", "Group", "Tracked", "Score", "Seeding"):
        table.add_column(col)
    table.add_row(
        "A (torrent)", fmt_resolution(sample_a), fmt_source(sample_a), fmt_video_codec(sample_a),
        hdr_a, audio_a, fmt_size(total_a), fmt_release_group(sample_a),
        sample_a.tracked_by, fmt_score(sample_a), fmt_seeding(sample_a),
    )
    table.add_row(
        "B", fmt_resolution(sample_b), fmt_source(sample_b), fmt_video_codec(sample_b),
        hdr_b, audio_b, fmt_size(total_b), fmt_release_group(sample_b),
        sample_b.tracked_by, fmt_score(sample_b), fmt_seeding(sample_b),
    )
    console.print(table)
    for _, a, _b in items:
        console.print(f"  [dim]{a.group_title}[/dim]")
    console.print("  [dim]Side A and B shown from episode 1 as representative — per-episode quality can vary slightly within a season pack.[/dim]")
    if any(h["audio"] or h["dv"] for h in highlights.values()):
        console.print("  [dim][bold green]green[/bold green] Audio/HDR-DV cell = notably better audio codec or a more broadly-supported DV profile (suppressed when a 4K Remux is in the group)[/dim]")
    console.print()


def main():
    if "--execute" in sys.argv:
        execute_plan(sys.argv[sys.argv.index("--execute") + 1])
        return
    report_only = "--report-only" in sys.argv
    reset = "--reset" in sys.argv
    use_trash = "--trash" in sys.argv
    # Off by default: for a library where Radarr/Sonarr's stale "still has the file" bookkeeping
    # is being used on purpose to stop automatic search from re-grabbing (or worse, silently
    # "upgrading" away) a release you deliberately kept — see the comment on maybe_reconcile()
    # below. Pass --reconcile to opt in to asking Radarr/Sonarr to import the kept file instead.
    use_reconcile = "--reconcile" in sys.argv

    missing = [n for n, v in [("RADARR_KEY", RADARR_KEY), ("SONARR_KEY", SONARR_KEY), ("QBIT_PASS", QBIT_PASS)] if not v]
    if missing:
        console.print(f"[bold red]Missing required credentials:[/bold red] {', '.join(missing)}")
        console.print("Copy .env.example to .env in this directory, fill them in, then run via ./run.sh")
        sys.exit(1)

    console.print(Panel("[bold]Plex Library Duplicate Review[/bold]\nFuzzy/quality duplicates — genuinely different files, not exact-match dupes", style="magenta"))

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TimeElapsedColumn(), console=console,
    ) as progress:
        t1 = progress.add_task("Scanning Plex library...", total=None)
        radarr_idx = load_radarr_index()
        sonarr_idx = load_sonarr_index(progress, progress.add_task("Indexing Sonarr episode files...", total=None))
        qbit_index = load_qbit_index()
        groups = build_groups(radarr_idx, sonarr_idx, qbit_index, progress, t1)
    require_qbit_for_apply()

    console.print(f"\n[bold]{len(groups)}[/bold] titles have more than one version in your library.\n")
    if use_trash:
        console.print("[dim]--trash is retired: removals now go through safe_ops (raw-disk unlink / qBittorrent API) and are dry-run by default.[/dim]\n")
    console.print(f"[bold]{'APPLY mode - each removal asks for y/N before it runs.' if APPLY else 'DRY RUN - choices are recorded in a plan; nothing is changed. Add --apply to execute as you go, or run a saved plan with --execute PLAN.json --apply.'}[/bold]\n")
    if use_reconcile:
        console.print("[dim]--reconcile active: keeping a loose file over a Radarr/Sonarr-tracked one will ask that app to import the kept file. Skip this flag if you'd rather that title's record stay stale (stops automatic search from re-grabbing it).[/dim]\n")

    done = set() if reset else load_state()
    total_freed = 0
    total_deleted = 0
    kept_all = 0
    run_stamp = time.strftime("%Y%m%d-%H%M%S")

    movie_groups = {k: v for k, v in groups.items() if k.startswith("movie:")}
    ep_groups = {k: v for k, v in groups.items() if k.startswith("episode:")}
    console.print(f"  Movies: {len(movie_groups)}   TV episodes: {len(ep_groups)}")
    if done:
        console.print(f"  [dim]{len(done)} already reviewed in a previous session (resuming) — pass --reset to start over[/dim]")
    console.print()

    quit_requested = False

    def maybe_reconcile(to_keep: list[FileRecord], to_delete_rec: FileRecord):
        # Off by default — see the --reconcile comment in main() for why. Leaving Radarr/Sonarr's record
        # stale (still believing it has the file you just deleted) is not a bug for every
        # setup: if the stale record's remembered quality already meets your quality profile's
        # cutoff, that app's automatic search skips the title entirely — no missing-search, no
        # upgrade-search. That can be exactly what you want when you've deliberately kept a
        # release you judge better than anything currently gettable, and don't want it silently
        # replaced (or unmonitored into a permanent gap) the next time something rescans it.
        # Pass --reconcile only if you actually want this tool to un-stale that bookkeeping by
        # asking the app to import the file you kept.
        if not use_reconcile:
            return
        if len(to_keep) != 1 or to_delete_rec.tracked_by not in ("Radarr", "Sonarr"):
            return
        if to_keep[0].tracked_by in ("Radarr", "Sonarr"):
            return  # kept file is itself tracked — nothing orphaned
        reconcile_arr(to_keep[0], to_delete_rec)

    # --- Season batches first: episodes sharing one torrent on one side get one decision ---
    clusters = build_season_clusters(groups)
    clusters = [c for c in clusters if not all(state_key(k, [a, b]) in done for k, a, b in c["items"])]
    if clusters and not report_only:
        console.print(f"[bold]{len(clusters)}[/bold] season batch(es) detected — review these first, then individual titles.\n")

    for idx, cluster in enumerate(clusters, start=1):
        render_season_cluster(cluster, position_label=f"[Batch {idx}/{len(clusters)}]")

        if report_only:
            for k, a, b in cluster["items"]:
                done.add(state_key(k, [a, b]))
            continue

        while True:
            choice = Prompt.ask(
                f"[bold]Keep[/bold] side A or B for all {len(cluster['items'])} episodes? 'a'/'b', 'i'=review individually, 'q'=quit"
            ).strip().lower()
            console.print(f"  [dim](received: {choice!r})[/dim]")
            if choice:
                break
            console.print("  [red]No input received.[/red]")

        if choice == "q":
            save_state(done)
            quit_requested = True
            break

        if choice == "i":
            console.print("  [dim]→ Will review these episodes individually below.[/dim]\n")
            continue

        if choice not in ("a", "b"):
            console.print(f"[red]'{choice}' isn't a valid choice — this batch will come up again next run.[/red]\n")
            continue

        cluster_failure = False
        for key, side_a, side_b in cluster["items"]:
            sk = state_key(key, [side_a, side_b])
            if sk in done:
                # Already resolved — e.g. handled individually in a prior run after picking
                # 'i' on this same cluster, or already deleted before an earlier quit. Without
                # this, re-picking 'a'/'b' would re-attempt os.remove() on an already-deleted
                # file and log a spurious failure for an episode that's actually fine.
                continue
            to_keep, to_delete = ([side_a], [side_b]) if choice == "a" else ([side_b], [side_a])
            outcome = delete_one(to_delete[0], to_keep, use_trash, run_stamp)
            if outcome in ("deleted", "planned"):
                total_deleted += 1
                total_freed += to_delete[0].size
                done.add(sk)
                maybe_reconcile(to_keep, to_delete[0])
            elif outcome in ("declined", "tagged"):
                done.add(sk)
            else:
                cluster_failure = True
        save_state(done)
        console.print(f"  [bold]→ Season batch done.[/bold]" + ("  [red](some files failed — those episodes will reappear next run)[/red]" if cluster_failure else "") + "\n")

    ordered_keys = sorted(groups.keys(), key=lambda k: groups[k][0].group_title)

    for idx, key in enumerate(ordered_keys, start=1):
        if quit_requested:
            break
        recs = groups[key]
        sk = state_key(key, recs)
        if sk in done:
            continue
        suggested = suggest_index(recs)
        render_group(console, recs[0].group_title, recs, suggested, position_label=f"[Title {idx}/{len(ordered_keys)}]")

        if report_only:
            done.add(sk)
            continue

        while True:
            choice = Prompt.ask(
                f"[bold]Keep[/bold] which number(s)? e.g. '1' or '1,3'  —  'a'=keep all, 'q'=quit"
            ).strip().lower()
            console.print(f"  [dim](received: {choice!r})[/dim]")
            if choice:
                break
            console.print("  [red]No input received — type 'a' to keep all, or a number to keep.[/red]")

        if choice == "q":
            save_state(done)
            break

        if choice == "a":
            done.add(sk)
            kept_all += 1
            save_state(done)
            console.print("  [dim]→ Kept all — nothing deleted for this title.[/dim]\n")
            continue

        try:
            keep_indices = {int(x.strip()) - 1 for x in choice.split(",") if x.strip()}
            if not keep_indices or any(i < 0 or i >= len(recs) for i in keep_indices):
                raise ValueError
        except ValueError:
            console.print(f"[red]'{choice}' isn't a valid number 1-{len(recs)} — nothing changed, this title will come up again next run.[/red]\n")
            continue

        to_delete = [r for i, r in enumerate(recs) if i not in keep_indices]
        to_keep = [recs[i] for i in sorted(keep_indices)]

        console.print(f"  [bold]→ Deleting {len(to_delete)}, keeping {len(to_keep)}:[/bold]")
        for rec in to_delete:
            console.print(f"    [red]✗ delete[/red]  {rec.path}")
        for rec in to_keep:
            console.print(f"    [green]✓ keep[/green]    {rec.path}")

        had_failure = False
        for rec in to_delete:
            outcome = delete_one(rec, to_keep, use_trash, run_stamp)
            if outcome in ("deleted", "planned"):
                total_deleted += 1
                total_freed += rec.size
                maybe_reconcile(to_keep, rec)
            elif outcome in ("unmapped", "error"):
                had_failure = True

        if had_failure:
            console.print("  [red]Not marking this title reviewed — at least one delete failed, it will come up again next run.[/red]\n")
            continue

        done.add(sk)
        save_state(done)
        console.print()

    if APPLY and not report_only and total_deleted > 0:
        console.print()
        plex_refresh_and_empty_trash()
    if PLAN and not APPLY:
        plan_path = f"{PLAN_DIR}/dupe-review-plan-{run_stamp}.json"
        safe_ops.save_plan(plan_path, PLAN)
        console.print(f"[bold green]DRY RUN: nothing was changed.[/bold green] {len(PLAN)} planned action(s) saved to {plan_path}\n  Validate:  ./run.sh review --execute {plan_path}\n  Execute:   ./run.sh review --execute {plan_path} --apply")

    console.print(Panel(
        f"{'Removed' if APPLY else 'Planned (dry run)'}: {total_deleted} files, {'freed' if APPLY else 'would free'} {fmt_size(total_freed)}\n"
        f"Kept-both/skipped: {kept_all}\n\n"
        + ("[bold]Remember:[/bold] run a Plex library scan + Empty Trash to stop these from showing as duplicate versions." if not PLEX_TOKEN or report_only or total_deleted == 0 else "Plex was refreshed automatically above."),
        title="Session summary", style="cyan",
    ))


if __name__ == "__main__":
    main()
