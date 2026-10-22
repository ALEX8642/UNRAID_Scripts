#!/usr/bin/env python3
"""
orphan-scan.py

Finds video files sitting on disk under Movies/tv that are referenced by NEITHER
qBittorrent (no torrent claims them) NOR Plex (not in its library database) — genuine dead
weight: failed imports, leftover artifacts from a botched move, orphaned partial downloads,
files left behind by a renamed/re-organized torrent.

Report-only. Never deletes anything — every candidate needs a human look. This is a
heuristic with real false-positive risks: a file Plex hasn't rescanned yet, a torrent added
after this scan started, or a file mid-write. Recently-modified files are flagged separately
rather than reported as confidently orphaned.
"""

import os
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich import box

from common import to_host_path
import safe_ops

# ==== CONFIG — non-secret settings, edit directly for your setup ====
PLEX_DB = (
    "/plexdata/Plex Media Server/Plug-in Support/Databases/"
    "com.plexapp.plugins.library.db"
)

MOVIES_HOST_ROOT = "/mnt/user/Media/Movies"
TV_HOST_ROOT = "/mnt/user/Media/tv"

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".iso", ".m4v", ".wmv"}
MIN_SIZE_BYTES = 10_000_000  # 10MB floor — excludes stray thumbnail/artifact files, not samples
RECENT_HOURS = 24  # files newer than this are flagged, not confidently called orphaned

LOGFILE = "/logs/orphan-scan.log"
MAX_DELETE_FILES = int(os.environ.get("ORPHAN_MAX_FILES", "25"))          # per --apply run: more than this looks like a broken index, not 25 orphans
MAX_DELETE_BYTES = int(float(os.environ.get("ORPHAN_MAX_GB", "300")) * 1024**3)
# ==== END CONFIG ====

# ==== CONFIG — credentials: set via a .env file (see .env.example), never hardcode here ====
QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
# ==== END CONFIG ====

console = Console()


def log(msg: str):
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    # os.walk() returns filesystem paths with invalid bytes surrogate-escaped (Python's normal
    # approach for undecodable filenames); writing them back out needs the same error handler.
    with open(LOGFILE, "a", errors="surrogateescape") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def scan_disk(progress, task) -> dict:
    """host path -> (size, mtime) for every video file under Movies/tv."""
    files = {}
    for root in (MOVIES_HOST_ROOT, TV_HOST_ROOT):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size < MIN_SIZE_BYTES:
                    continue
                files[full] = (st.st_size, st.st_mtime)
                progress.advance(task)
    return files


def load_plex_known() -> set:
    uri = f"file:{PLEX_DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    # A handful of filenames in the wild have invalid-UTF8 bytes (odd characters from a scene
    # release name); sqlite3's default TEXT decoding raises on those. Tolerate them instead of
    # crashing the whole scan over one unrelated row.
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    cur = con.cursor()
    cur.execute("SELECT file FROM media_parts")
    known = set()
    for row in cur.fetchall():
        if not row[0]:
            continue
        p, mapped = to_host_path(row[0])
        if mapped:
            known.add(p)
    con.close()
    return known


def load_qbit_known() -> set:
    s = requests.Session()
    login = s.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=15)
    if login.status_code != 200 or login.text.strip() != "Ok.":
        console.print("[bold red]FATAL:[/bold red] could not log into qBittorrent. Refusing to run — without this index, everything would look orphaned.")
        sys.exit(1)
    r = s.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=30)
    r.raise_for_status()
    torrents = r.json()
    if not torrents:
        console.print("[bold red]FATAL:[/bold red] qBittorrent reports zero torrents — treating as a fetch problem, not \"nothing is seeding\". Refusing to run.")
        sys.exit(1)

    known = set()
    for t in torrents:
        cp, cp_mapped = to_host_path((t.get("content_path", "") or "").rstrip("/"))
        if cp_mapped:
            known.add(cp)
        save_path, sp_mapped = to_host_path((t.get("save_path", "") or "").rstrip("/"))
        if not sp_mapped:
            continue
        rr = s.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=30)
        if not rr.ok:
            continue
        for f in rr.json():
            known.add(os.path.join(save_path, f["name"]))
    return known


def load_arr_known() -> set:
    """Every file Radarr or Sonarr tracks. A tracked library file is never an orphan, even if Plex has not indexed it
    and no torrent claims it (Plex excluded/unscanned, or a loose import)."""
    known = set()
    for url, key, kind in ((RADARR_URL, RADARR_KEY, "radarr"), (SONARR_URL, SONARR_KEY, "sonarr")):
        if not key:
            console.print(f"[bold red]FATAL:[/bold red] {kind.upper()}_KEY not set. Refusing to run: tracked files must be excluded from the orphan list.")
            sys.exit(1)
        h = {"X-Api-Key": key}
        if kind == "radarr":
            r = requests.get(f"{url}/api/v3/movie", headers=h, timeout=120)
            r.raise_for_status()
            paths = [m["movieFile"]["path"] for m in r.json() if m.get("movieFile")]
        else:
            r = requests.get(f"{url}/api/v3/series", headers=h, timeout=120)
            r.raise_for_status()
            paths = []
            for s_ in r.json():
                rr = requests.get(f"{url}/api/v3/episodefile", params={"seriesId": s_["id"]}, headers=h, timeout=120)
                rr.raise_for_status()
                paths += [f["path"] for f in rr.json()]
        for p in paths:
            hp, mapped = to_host_path(p)
            if mapped:
                known.add(hp)
    return known


def find_orphans(on_disk: dict, plex_known: set, qbit_known: set, arr_known: set) -> dict:
    """Files claimed by none of Plex, qBittorrent, Radarr or Sonarr."""
    return {p: v for p, v in on_disk.items() if p not in plex_known and p not in qbit_known and p not in arr_known}


def sanity_problem(on_disk: dict, plex_known: set, qbit_known: set, arr_known: set):
    """A reason the indexes cannot be trusted (so --apply must refuse), or None. A wrong Plex DB path, an unmapped
    mount or an app returning nothing would otherwise make every file look orphaned."""
    if not on_disk:
        return "no video files found on disk (library roots unreadable?)"
    if not qbit_known:
        return "qBittorrent index is empty"
    if not arr_known:
        return "Radarr/Sonarr index is empty"
    if len(plex_known) < 0.5 * len(on_disk):
        return f"Plex index looks wrong: {len(plex_known)} known paths for {len(on_disk)} files on disk (path mapping or DB location?)"
    return None


def fmt_size(n: int) -> str:
    return f"{n / (1024**3):.2f} GB"


def main():
    apply = "--apply" in sys.argv
    exclude = set()
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--exclude" and i + 1 < len(argv):
            exclude.add(argv[i + 1])

    console.print(
        "[bold]Orphan File Scan[/bold]\nFiles on disk under Movies/tv claimed by neither qBittorrent nor Plex.\n"
        + ("[bold red]APPLY MODE — matching files will be deleted (file-level only, never a directory).[/bold red]"
           if apply else "Report-only — nothing is ever deleted by this tool."),
        style="magenta",
    )

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TimeElapsedColumn(), console=console,
    ) as progress:
        t1 = progress.add_task("Indexing qBittorrent torrents...", total=None)
        qbit_known = load_qbit_known()
        progress.update(t1, total=1, completed=1)

        t2 = progress.add_task("Indexing Plex library...", total=None)
        plex_known = load_plex_known()
        progress.update(t2, total=1, completed=1)

        t3 = progress.add_task("Scanning disk for video files...", total=None)
        on_disk = scan_disk(progress, t3)

    arr_known = load_arr_known()
    problem = sanity_problem(on_disk, plex_known, qbit_known, arr_known)
    if problem:
        console.print(f"[bold red]Refusing to continue:[/bold red] {problem}. Nothing was listed or deleted.")
        log(f"REFUSED: {problem}")
        return 1
    not_in_plex = {p: v for p, v in on_disk.items() if p not in plex_known}
    orphans = find_orphans(on_disk, plex_known, qbit_known, arr_known)

    console.print(f"\nOn disk: [bold]{len(on_disk)}[/bold]  Plex-known: [bold]{len(plex_known)}[/bold]  qBittorrent-known: [bold]{len(qbit_known)}[/bold]")
    # The raw counts above don't subtract cleanly into a meaningful number — Plex's own
    # known-set can contain stale entries for files no longer on disk, which masks the real
    # gap. Spell out the actual breakdown instead of leaving it to be inferred (wrongly) from
    # the three totals: a user comparing 14834 vs 14832 above once read that as "2 files
    # unaccounted for" when the real number of on-disk-but-not-in-Plex files was 26, all of
    # them still claimed by qBittorrent and therefore correctly not orphans.
    if not_in_plex:
        console.print(
            f"{len(not_in_plex)} file(s) on disk aren't in Plex yet — "
            f"{len(not_in_plex) - len(orphans)} of those are still claimed by qBittorrent or tracked by Radarr/Sonarr "
            f"(not orphans), {len(orphans)} genuinely unclaimed by either.\n"
        )
    else:
        console.print()

    if not orphans:
        console.print("[green]No orphaned files found.[/green]")
        return 0

    now = time.time()
    rows = sorted(orphans.items(), key=lambda kv: kv[1][0], reverse=True)

    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Size")
    table.add_column("Age")
    table.add_column("Path")
    if apply:
        table.add_column("Result")

    removed_raw = []
    fresh = None
    if apply:
        # the scan took minutes: ask qBittorrent again right before deleting, so a torrent added meanwhile is respected
        fresh = find_orphans(on_disk, plex_known, load_qbit_known(), load_arr_known())
    total_size = 0
    recent_count = 0
    deleted_count = 0
    deleted_size = 0
    excluded_count = 0
    failed_count = 0
    for path, (size, mtime) in rows:
        age_hours = (now - mtime) / 3600
        recent = age_hours < RECENT_HOURS
        if recent:
            recent_count += 1
        age_label = f"[yellow]{age_hours:.0f}h — recent, may not be indexed yet[/yellow]" if recent else f"{age_hours / 24:.0f}d"
        total_size += size

        if not apply:
            table.add_row(fmt_size(size), age_label, path)
            continue

        if path in exclude:
            result = "[yellow]excluded[/yellow]"
            excluded_count += 1
            log(f"EXCLUDED (--exclude): {path}")
        elif recent:
            result = "[yellow]skipped (recent)[/yellow]"
        elif path not in fresh:
            result = "[yellow]skipped (claimed since the scan started)[/yellow]"
            log(f"SKIPPED {path}: claimed by qBittorrent/Radarr/Sonarr since the scan")
        elif deleted_count >= MAX_DELETE_FILES or deleted_size + size > MAX_DELETE_BYTES:
            result = "[yellow]skipped (per-run limit reached; investigate before raising it)[/yellow]"
            log(f"LIMIT {path}: per-run limit of {MAX_DELETE_FILES} files / {fmt_size(MAX_DELETE_BYTES)} reached")
        else:
            # Never write through /mnt/user: resolve the physical disk copy and unlink it there. Refuse anything
            # that is hardlinked elsewhere (shared data: some other name, e.g. a seed in arr/, still needs it)
            # or that exists on more than one disk (needs cross-disk-dedupe first).
            raw = safe_ops.raw_copies(path)
            try:
                if len(raw) != 1:
                    result = f"[yellow]skipped ({len(raw)} physical copies)[/yellow]"
                    log(f"SKIPPED {path}: {len(raw)} physical copies")
                elif safe_ops.file_identity(raw[0])[2] > 1:
                    result = "[yellow]skipped (hardlinked elsewhere)[/yellow]"
                    log(f"SKIPPED {path}: hardlinked elsewhere")
                else:
                    safe_ops.unlink_raw(raw, True, LOGFILE, "orphan-scan")
                    removed_raw.append(raw[0])
                    result = "[green]deleted[/green]"
                    deleted_count += 1
                    deleted_size += size
            except OSError as e:
                result = f"[red]failed: {e}[/red]"
                failed_count += 1
                log(f"FAILED to delete {path}: {e}")
        table.add_row(fmt_size(size), age_label, path, result)

    console.print(table)

    if apply:
        for d in safe_ops.prune_empty_parents(removed_raw, True, logfile=LOGFILE):
            log(f"PRUNED empty folder {d}")
        console.print(
            f"\n[bold]Deleted {deleted_count} file(s), freed {fmt_size(deleted_size)}.[/bold]  "
            f"Skipped {recent_count} recent, {excluded_count} excluded, {failed_count} failed."
        )
        log(f"APPLY RUN: deleted {deleted_count} ({fmt_size(deleted_size)}), skipped {recent_count} recent, {excluded_count} excluded, {failed_count} failed")
    else:
        console.print(f"\n[bold]{len(orphans)}[/bold] candidate orphan file(s), [bold]{fmt_size(total_size)}[/bold] total"
                      + (f" — [yellow]{recent_count} are under {RECENT_HOURS}h old, verify those aren't just unindexed yet[/yellow]" if recent_count else ""))
        console.print("\n[dim]Nothing was deleted. Review each path — a file Plex hasn't rescanned yet, or a torrent added after this scan started, would look identical to genuine dead weight.[/dim]")
        log(f"Scan: {len(orphans)} candidates, {fmt_size(total_size)} total, {recent_count} flagged recent")
        for path, (size, mtime) in rows:
            log(f"CANDIDATE: {path} ({fmt_size(size)}, mtime age {(now - mtime) / 3600:.1f}h)")

    return len(orphans)


if __name__ == "__main__":
    main()
