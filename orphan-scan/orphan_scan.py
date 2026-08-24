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

# ==== CONFIG — non-secret settings, edit directly for your setup ====
PLEX_DB = (
    "/plexdata/Plex Media Server/Plug-in Support/Databases/"
    "com.plexapp.plugins.library.db"
)

MOVIES_HOST_ROOT = "/mnt/user/Media/Movies"
TV_HOST_ROOT = "/mnt/user/Media/tv"

# Container-path -> host-path prefixes, same mapping used by dedupe-library.sh / dupe-review.
PATH_MAP = [
    ("/movies/", "/mnt/user/Media/Movies/"),
    ("/tv/", "/mnt/user/Media/tv/"),
    ("/arr/movies/", "/mnt/user/Media/arr/movies/"),
    ("/arr/shows/", "/mnt/user/Media/arr/shows/"),
]

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".iso", ".m4v", ".wmv"}
MIN_SIZE_BYTES = 10_000_000  # 10MB floor — excludes stray thumbnail/artifact files, not samples
RECENT_HOURS = 24  # files newer than this are flagged, not confidently called orphaned

LOGFILE = "/logs/orphan-scan.log"
# ==== END CONFIG ====

# ==== CONFIG — credentials: set via a .env file (see .env.example), never hardcode here ====
QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
# ==== END CONFIG ====

console = Console()


def to_host_path(p: str) -> str:
    for prefix, host in PATH_MAP:
        if p.startswith(prefix):
            return host + p[len(prefix):]
    return p


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
    known = {to_host_path(row[0]) for row in cur.fetchall() if row[0]}
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
        cp = to_host_path((t.get("content_path", "") or "").rstrip("/"))
        known.add(cp)
        save_path = to_host_path((t.get("save_path", "") or "").rstrip("/"))
        rr = s.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=30)
        if not rr.ok:
            continue
        for f in rr.json():
            known.add(os.path.join(save_path, f["name"]))
    return known


def fmt_size(n: int) -> str:
    return f"{n / (1024**3):.2f} GB"


def sweep_empty_dirs():
    for root in (MOVIES_HOST_ROOT, TV_HOST_ROOT):
        subprocess.run(["find", root, "-mindepth", "1", "-type", "d", "-empty", "-delete"], check=False)


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

    console.print(f"\nOn disk: [bold]{len(on_disk)}[/bold]  Plex-known: [bold]{len(plex_known)}[/bold]  qBittorrent-known: [bold]{len(qbit_known)}[/bold]\n")

    orphans = {p: v for p, v in on_disk.items() if p not in plex_known and p not in qbit_known}

    if not orphans:
        console.print("[green]No orphaned files found.[/green]")
        return

    now = time.time()
    rows = sorted(orphans.items(), key=lambda kv: kv[1][0], reverse=True)

    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("Size")
    table.add_column("Age")
    table.add_column("Path")
    if apply:
        table.add_column("Result")

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
        else:
            try:
                os.remove(path)
                result = "[green]deleted[/green]"
                deleted_count += 1
                deleted_size += size
                log(f"DELETED: {path} ({fmt_size(size)})")
            except OSError as e:
                result = f"[red]failed: {e}[/red]"
                failed_count += 1
                log(f"FAILED to delete {path}: {e}")
        table.add_row(fmt_size(size), age_label, path, result)

    console.print(table)

    if apply:
        sweep_empty_dirs()
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


if __name__ == "__main__":
    main()
