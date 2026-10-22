#!/usr/bin/env python3
"""
quarantine.py - list, restore and purge the files the mothership tools "removed".

Removals never delete: safe_ops moves a file to <disk>/Media/.quarantine/<YYYY-MM-DD>/<path under Media> (a same-disk rename).
Plex, Radarr, Sonarr and qBittorrent do not look there. Nothing is lost until `purge --apply` empties old days.

  python3 quarantine.py list                       # every quarantined file, newest first, with size
  python3 quarantine.py restore DATE/REL [--apply] # move it back to Media/REL on the same disk (never overwrites)
  python3 quarantine.py purge [--days 7] [--apply] # delete day folders older than N days (dry run by default)
"""
import datetime
import glob
import os
import re
import shutil
import sys

MNT = os.environ.get("MOTHERSHIP_MNT", "/mnt")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def roots():
    return sorted(glob.glob(os.path.join(MNT, "disk[0-9]*")) + glob.glob(os.path.join(MNT, "cache")))


def days(root):
    q = os.path.join(root, "Media", ".quarantine")
    if not os.path.isdir(q):
        return []
    return sorted(d for d in os.listdir(q) if DAY.match(d) and os.path.isdir(os.path.join(q, d)))


def size_of(path):
    total = 0
    for r, _d, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def listing():
    out = []
    for root in roots():
        for d in days(root):
            base = os.path.join(root, "Media", ".quarantine", d)
            for r, _dn, fs in os.walk(base):
                for f in fs:
                    p = os.path.join(r, f)
                    out.append({"root": root, "day": d, "rel": os.path.relpath(p, base), "size": os.path.getsize(p), "path": p})
    return sorted(out, key=lambda x: (x["day"], x["rel"]), reverse=True)


def purge(older_than_days, apply, today=None):
    today = today or datetime.date.today()
    freed, gone = 0, []
    for root in roots():
        for d in days(root):
            age = (today - datetime.date.fromisoformat(d)).days
            if age < older_than_days:
                continue
            p = os.path.join(root, "Media", ".quarantine", d)
            assert os.path.basename(os.path.dirname(p)) == ".quarantine" and DAY.match(os.path.basename(p))     # never anything but a dated day folder
            freed += size_of(p)
            gone.append(p)
            if apply:
                shutil.rmtree(p)
    return gone, freed


def restore(day_rel, apply):
    day, _, rel = day_rel.partition("/")
    if not DAY.match(day) or not rel or ".." in rel.split("/"):
        raise SystemExit("usage: restore YYYY-MM-DD/path/under/Media")
    hits = [r for r in roots() if os.path.isfile(os.path.join(r, "Media", ".quarantine", day, rel))]
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one quarantined copy, found {len(hits)}")
    src = os.path.join(hits[0], "Media", ".quarantine", day, rel)
    dst = os.path.join(hits[0], "Media", rel)
    if os.path.lexists(dst):
        raise SystemExit(f"{dst} already exists; not overwriting")
    print(f"{'RESTORE' if apply else 'WOULD RESTORE'} {src} -> {dst}")
    if apply:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    apply = "--apply" in argv
    if cmd == "list":
        for e in listing():
            print(f"{e['day']}  {e['size'] / 1e9:8.2f} GB  {e['root']}  {e['rel']}")
    elif cmd == "purge":
        n = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
        gone, freed = purge(n, apply)
        print(f"{'PURGED' if apply else 'WOULD PURGE'} {len(gone)} day folder(s) older than {n} days, {freed / 1e9:.1f} GB")
    elif cmd == "restore":
        restore(argv[2], apply)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
