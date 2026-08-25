#!/usr/bin/env python3
"""
mothership.py

Single entry point for the three library-maintenance tools in this repo:
  1. cross-disk-dedupe.sh — finds files that physically exist twice across two different
     array disks (invisible to every other tool here, which all read through /mnt/user)
  2. dupe-review.py       — interactive review of titles that exist as more than one genuinely
     different file (quality/source/encode)
  3. orphan-scan.py       — files on disk claimed by neither Plex nor qBittorrent

This runs each tool's own, already-tested code completely unchanged — it only picks which
one(s) to run, prompts for the args each one already supports, and (for "run all") sequences
them in the order that makes sense: cheap byte-identical wins first, then the judgment calls,
then a final sweep for anything left behind by either.

dedupe-library.sh (loose file vs. Radarr/Sonarr-sorted copy, matched by same inode) was
retired 2026-08-25 — its still-hardlinked cleanup caused a real incident (deleting one name of
a hardlinked pair broke qBittorrent's own path-based tracking) and its remaining purpose is
fully superseded by cross-disk-dedupe.sh's more careful checks. It's gone from the repo, not
just disabled.
"""

import os
import re
import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

console = Console()

sys.path.insert(0, "/app")
CROSS_DISK_DEDUPE_SH = "/app/cross-disk-dedupe.sh"


def run_cross_disk_dedupe_sh(apply: bool) -> int:
    """Runs cross-disk-dedupe.sh, streaming its output live (plain print, not console.print —
    its lines start with a bracketed timestamp, which rich would otherwise try to parse as a
    markup tag). Returns the total resolved count parsed out of its summary line, so the
    caller can skip asking to apply when nothing was actually found."""
    args = ["bash", CROSS_DISK_DEDUPE_SH] + (["--apply"] if apply else [])
    console.print(Panel(f"[bold]cross-disk-dedupe.sh[/bold] — {'APPLY' if apply else 'dry run'}", style="cyan"))
    total_found = 0
    proc = subprocess.Popen(
        args, env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
        m = re.search(r"summary: (\d+) resolved", line)
        if m:
            total_found += int(m.group(1))
    proc.wait()
    return total_found


def cross_disk_dedupe_flow():
    console.print(Panel("[bold]Cross-Disk Duplicate Cleanup[/bold]\nFiles that physically exist on two different array disks at the same library path — invisible to every other tool here. Always previews before deleting anything.", style="magenta"))
    found = run_cross_disk_dedupe_sh(apply=False)
    if found == 0:
        console.print("\n[green]Nothing to clean up — no cross-disk duplicates found.[/green]")
        return
    if Confirm.ask(f"\n{found} duplicate(s) found — apply these deletions now?", default=False):
        run_cross_disk_dedupe_sh(apply=True)


def call_module_main(module_name: str, argv: list[str]):
    """Imports/reuses the module, temporarily swaps sys.argv, and calls its main() — catching
    SystemExit so a guard clause inside that tool (missing creds, qBittorrent down, etc.)
    returns control to this menu instead of killing the whole mothership process. Returns
    whatever that tool's main() returns (orphan_scan's does: the candidate count found),
    or None if it exited early."""
    module = sys.modules.get(module_name)
    if module is None:
        import importlib
        module = importlib.import_module(module_name)
    old_argv = sys.argv
    sys.argv = [f"{module_name}.py"] + argv
    try:
        return module.main()
    except SystemExit as e:
        if e.code not in (0, None):
            console.print(f"[yellow]{module_name} exited early (code {e.code}) — see message above.[/yellow]")
        return None
    finally:
        sys.argv = old_argv


def dupe_review_flow():
    console.print(Panel(
        "[bold]Quality-Based Duplicate Review[/bold]\n"
        "Finds titles that exist as more than one genuinely different file (different "
        "resolution/source/encode) and helps you pick which to keep.",
        style="magenta",
    ))
    argv = []
    if Confirm.ask(
        "\nJust show a report of what it finds, without touching anything or asking about "
        "each title? (Answer 'n' to instead go through titles one at a time and decide what "
        "to keep)",
        default=False,
    ):
        argv.append("--report-only")
    else:
        if Confirm.ask(
            "\nWhen you choose to delete a file, move it to logs/trash/ instead of deleting "
            "it outright, so you can still recover it if you change your mind?",
            default=False,
        ):
            argv.append("--trash")
        if Confirm.ask(
            "\nAfter you keep a file, also ask Radarr/Sonarr to re-import it so it's tracked "
            "again? (Leave this off if you're relying on the old, untracked record to stop "
            "an automatic re-download)",
            default=False,
        ):
            argv.append("--reconcile")
    if Confirm.ask(
        "\nThis tool remembers titles you've already decided on and normally only shows you "
        "new ones. Re-check everything from scratch, including titles already reviewed?",
        default=False,
    ):
        argv.append("--reset")
    call_module_main("dupe_review", argv)


def orphan_scan_flow():
    console.print(Panel("[bold]Orphan File Scan[/bold]\nFiles claimed by neither Plex nor qBittorrent. Always scans in report-only mode first.", style="magenta"))
    found = call_module_main("orphan_scan", [])
    if not found:
        return
    if Confirm.ask(f"\n{found} candidate(s) found — apply these deletions now?", default=False):
        extra = Prompt.ask("Any paths to exclude? (comma-separated, blank for none)", default="")
        argv = ["--apply"]
        for p in [x.strip() for x in extra.split(",") if x.strip()]:
            argv += ["--exclude", p]
        call_module_main("orphan_scan", argv)


def run_all():
    console.print(Panel(
        "[bold]Running all three in the recommended order:[/bold]\n"
        "1. Cross-disk duplicate cleanup (cheap, byte-identical wins)\n"
        "2. Quality-based duplicate review (judgment calls)\n"
        "3. Orphan scan (final sweep for anything left behind)",
        style="magenta",
    ))
    cross_disk_dedupe_flow()
    console.print()
    dupe_review_flow()
    console.print()
    orphan_scan_flow()
    console.print(Panel("[bold green]All three done.[/bold green]", style="green"))


MENU = {
    "1": ("Cross-disk duplicate cleanup (cross-disk-dedupe.sh)", cross_disk_dedupe_flow),
    "2": ("Quality-based duplicate review (dupe-review)", dupe_review_flow),
    "3": ("Orphan file scan", orphan_scan_flow),
    "4": ("Run all three, in the recommended order", run_all),
    "q": ("Quit", None),
}

SUBCOMMANDS = {"cross-disk-dedupe", "dupe-review", "orphan-scan"}


def run_subcommand(name: str, rest: list[str]):
    """Non-interactive entry point — runs one tool directly with the given args, no menu, no
    extra prompts beyond what that tool already asks on its own (e.g. dupe-review's per-title
    choices). For scripted/cron use, such as a scheduled `mothership.py cross-disk-dedupe
    --apply` run."""
    if name == "cross-disk-dedupe":
        run_cross_disk_dedupe_sh(apply="--apply" in rest)
    elif name == "dupe-review":
        call_module_main("dupe_review", rest)
    elif name == "orphan-scan":
        call_module_main("orphan_scan", rest)


def print_help():
    console.print(
        "[bold]Usage:[/bold]\n"
        "  mothership.py                          Interactive menu\n"
        "  mothership.py cross-disk-dedupe [--apply]\n"
        "  mothership.py dupe-review [--report-only] [--trash] [--reconcile] [--reset]\n"
        "  mothership.py orphan-scan [--apply] [--exclude PATH ...]\n\n"
        "Subcommands run directly with no extra menu prompts — for scripted/cron use (e.g. a\n"
        "scheduled 'mothership.py cross-disk-dedupe --apply' run). Omit the subcommand for\n"
        "the interactive menu."
    )


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print_help()
        return
    if argv and argv[0] in SUBCOMMANDS:
        run_subcommand(argv[0], argv[1:])
        return
    if argv:
        # An unrecognized first arg must error, not silently fall through to the interactive
        # menu — under cron (no stdin) that menu's Prompt.ask() would EOFError-crash with a
        # confusing traceback instead of a clear "you typed something wrong" message.
        console.print(f"[red]Unknown subcommand: {argv[0]!r}[/red]\n")
        print_help()
        sys.exit(1)

    console.print(Panel("[bold]Plex Library Maintenance[/bold]", style="magenta"))
    while True:
        console.print()
        for key, (label, _) in MENU.items():
            console.print(f"  [bold]{key}[/bold]. {label}")
        choice = Prompt.ask("\nChoice", choices=list(MENU.keys()), default="4")
        if choice == "q":
            break
        _, fn = MENU[choice]
        console.print()
        fn()


if __name__ == "__main__":
    main()
