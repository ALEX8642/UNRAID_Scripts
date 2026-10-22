"""
safe_ops.py - the safety layer for every destructive action the mothership tools take.

Why this exists (lessons from the 2026 library migration):
  * Never modify files through /mnt/user (Unraid's FUSE layer). Resolve to the physical
    /mnt/diskN path and act there.
  * A torrent's data is tracked by PATH. Unlinking the only name qBittorrent knows makes it
    think the data vanished (redownload / missingFiles). Seeded files go through qBittorrent's
    API, or - when the torrent may still be inside a tracker's hit-and-run window - only the
    Plex-visible library name is unlinked and the torrent is tagged for later removal.
  * Torrents whose files sit loose in the share root ("rootless": content_path == save_path)
    must be matched file by file, never by prefix.
  * Everything is dry-run unless apply=True, and every action is logged.

Pure decision logic (classify_removal) is separated from side effects (execute) so it can be
unit-tested without a server. See tests/test_safe_ops.py.
"""

import datetime
import glob
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

TAG_DELETE_LATER = "delete-later"
QUARANTINE = ".quarantine"      # <disk>/Media/.quarantine/<YYYY-MM-DD>/<path under Media>; purged by quarantine_purge.py after a week
# Conservative stand-in for tracker hit-and-run rules (which are members-only and vary):
# a torrent seeded for fewer days than this is treated as possibly still inside its window.
def _env_days(name, default, floor):
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        v = float(default)
    return max(v, floor)   # a typo of 0 or a negative number must never turn every seed into "seasoned"


HNR_MIN_SEED_DAYS = _env_days("HNR_MIN_SEED_DAYS", "30", 5.0)
BHD_RULE_DAYS = 5.0   # BeyondHD: seed 120 h OR ratio 1.0
SEEDING_STATES = ("uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP")
# Torrents with more than this many files are treated as packs: never deleted per file.
MAX_TORRENT_FILES = 3
# Where the physical array disks live (overridable for tests).
MNT = os.environ.get("MOTHERSHIP_MNT", "/mnt")
DISK_GLOB = os.environ.get("MOTHERSHIP_DISK_GLOB", MNT + "/disk[0-9]*")
USER_PREFIX = MNT + "/user/"
FUSE_VIEWS = ("user", "user0", "disks", "remotes", "rootshare")      # never modify files through any of these


def raw_copies(host_path: str, disk_glob: Optional[str] = None) -> list:
    """/mnt/user/Media/x -> every physical copy /mnt/diskN/Media/x that exists (plus the cache pool)."""
    disk_glob = disk_glob or DISK_GLOB
    if not host_path.startswith(USER_PREFIX):
        return []
    rel = host_path[len(USER_PREFIX):]
    if ".." in rel.split("/") or not rel:
        return []   # never resolve a path that climbs out of the share
    # Only the disk part is a pattern. A name like "Foo [2020]/a.mkv" must not be read as a character class, and "Who?.mkv"
    # must not match "Who!.mkv": that would unlink a different title's file.
    hits = [p for p in glob.glob(os.path.join(disk_glob, glob.escape(rel))) if os.path.lexists(p) and p.endswith("/" + rel)]
    cache = os.path.join(os.path.dirname(disk_glob), "cache", rel)
    if os.path.lexists(cache):
        hits.append(cache)
    return sorted(hits)


def file_identity(path: str) -> tuple:
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_nlink)


def other_names(raw_path: str, subdirs=("Media/arr",)) -> list:
    """Other hardlink names of the same file on the SAME physical disk, searched only under the given
    subdirectories of that disk (e.g. the arr/ download tree where Radarr/Sonarr seeds live)."""
    import subprocess
    st = os.stat(raw_path)
    if st.st_nlink < 2 or "/Media/" not in raw_path:
        return []
    root = raw_path.split("/Media/", 1)[0]
    found = []
    for sub in subdirs:
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            r = subprocess.run(["find", d, "-xdev", "-type", "f", "-inum", str(st.st_ino), "-print0"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"hardlink search failed in {d}: {r.stderr.strip()[:120]}")
            found += [l for l in r.stdout.split("\0") if l and l != raw_path]      # NUL-separated: a newline in a name must not split it
    return found


@dataclass
class TorrentInfo:
    hash: str
    name: str
    seed_days: float
    category: str = ""
    tracker: str = ""
    n_files: int = 1
    covers_exact_path: bool = True   # True when this torrent's own file IS the path being removed
    shared_with: int = 0             # how many OTHER torrents point at the same data (cross-seeding); deleting files would break them


@dataclass
class Decision:
    mode: str                         # see MODES
    reason: str
    unlink_raw: list = field(default_factory=list)   # physical library names to unlink
    delete_torrent: bool = False                      # then remove the torrent via qBittorrent's API
    delete_files: bool = False                        # ... and its files too (only when the data stays reachable through a quarantined name)
    tag: bool = False                                 # keep the torrent seeding, tag it delete-later
    torrent_hash: str = ""
    torrent_name: str = ""

    def as_dict(self):
        return asdict(self)


MODES = {
    "UNLINK": "no torrent owns this file: unlink the physical copy",
    "LIBRARY_NAME_THEN_DELETE": "library name is a hardlink of a seed kept elsewhere (arr/), seeded long enough: unlink the library name, then remove the torrent + files via qBittorrent",
    "LIBRARY_NAME_THEN_TAG": "library name is a hardlink of a seed kept elsewhere (arr/), possibly inside a hit-and-run window: unlink only the Plex-visible name, keep seeding, tag delete-later",
    "QBIT_DELETE": "the torrent's own tracked name, seeded long enough: quarantine the file, then remove the torrent from qBittorrent (files are NOT deleted by qBittorrent)",
    "TAG_ONLY": "the torrent's own tracked name and the window may be open: unlinking would break the seed, so only tag delete-later and leave the files (hide it later by relocating the seed)",
    "REFUSE": "refused for safety",
}


def seed_rule_met(t: dict, now: float, days: float = BHD_RULE_DAYS) -> bool:
    """BeyondHD: 120 h seeded OR ratio 1.0. `completion_on` is -1/0 for torrents that never completed, which must never read as
    "ancient". Wall-clock age only counts for a torrent that is actually seeding right now (or long gone: 14+ days), because a
    paused, stopped or stalled-on-missing-files torrent accrues no seeding time. qBittorrent's seeding_time is honoured too
    (fastresume edits reset it, which only ever makes this stricter)."""
    done = t.get("completion_on") or 0
    if done <= 0:
        return False
    if (t.get("ratio") or 0) >= 1.0:
        return True
    if (t.get("seeding_time") or 0) >= days * 86400:
        return True
    age_days = (now - done) / 86400
    return age_days >= max(days, 14.0) or (age_days >= days and t.get("state") in SEEDING_STATES)


def classify_removal(raw_paths: list, torrent: Optional[TorrentInfo], keeper_raw: str = "", hnr_days: Optional[float] = None) -> Decision:
    """Decide how to remove ONE losing file. Pure function: no I/O.

    raw_paths: physical copies of the losing path (raw_copies); torrent: the qBittorrent torrent that owns
    this file's data (None if none); torrent.covers_exact_path is True when the torrent's own tracked
    name is the path being removed, False when the torrent lives under another name (a seed in arr/) and
    the loser is only a hardlinked library name. keeper_raw: the kept file's physical path (never removed).
    """
    hnr_days = HNR_MIN_SEED_DAYS if hnr_days is None else hnr_days
    if not raw_paths:
        return Decision("REFUSE", "path does not resolve to a physical disk")
    if len(raw_paths) > 1:
        return Decision("REFUSE", f"path exists on {len(raw_paths)} physical disks - resolve with cross-disk-dedupe first")
    if keeper_raw and raw_paths[0] == keeper_raw:
        return Decision("REFUSE", "loser and keeper are the same physical file")
    if torrent is None:
        return Decision("UNLINK", MODES["UNLINK"], unlink_raw=list(raw_paths))
    base = dict(torrent_hash=torrent.hash, torrent_name=torrent.name)
    if torrent.n_files > MAX_TORRENT_FILES:
        return Decision("REFUSE", f"belongs to a {torrent.n_files}-file pack torrent; a partial delete would break the seed", **base)
    seasoned = torrent.seed_days >= hnr_days
    if torrent.shared_with and seasoned:
        # another torrent (another tracker, possibly a young BHD one) points at the very same files: deleting them would kill that seed
        return Decision("REFUSE", f"{torrent.shared_with} other torrent(s) share these files; deleting would break their seeding", **base)
    if not torrent.covers_exact_path:
        return Decision("LIBRARY_NAME_THEN_DELETE" if seasoned else "LIBRARY_NAME_THEN_TAG",
                        MODES["LIBRARY_NAME_THEN_DELETE" if seasoned else "LIBRARY_NAME_THEN_TAG"],
                        unlink_raw=list(raw_paths), delete_torrent=seasoned, delete_files=(seasoned and torrent.n_files == 1), tag=not seasoned, **base)   # only a single-file seed: its one file is the quarantined name; other files exist nowhere else
    if seasoned:
        return Decision("QBIT_DELETE", MODES["QBIT_DELETE"], unlink_raw=list(raw_paths), delete_torrent=True, delete_files=False, **base)
    return Decision("TAG_ONLY", MODES["TAG_ONLY"], tag=True, **base)


# ---------------------------------------------------------------------------
# Side effects (all gated by apply)
# ---------------------------------------------------------------------------

def _log(logfile: str, msg: str):
    if not logfile:
        return
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    with open(logfile, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def quarantine_path(p: str, today: str = "") -> str:
    """/mnt/disk3/Media/Movies/A/a.mkv -> /mnt/disk3/Media/.quarantine/2026-10-22/Movies/A/a.mkv (same disk, so a rename)."""
    if "/Media/" not in p:
        raise ValueError(f"not under a Media folder: {p}")
    root, rel = p.split("/Media/", 1)
    if rel.split("/", 1)[0] == QUARANTINE:
        raise ValueError("already in quarantine")
    today = today or datetime.date.today().isoformat()
    dest = os.path.join(root, "Media", QUARANTINE, today, rel)
    n, cand = 1, dest
    while os.path.lexists(cand):
        n += 1
        cand = f"{dest}.{n}"
    return cand


def unlink_raw(paths: list, apply: bool, logfile: str = "", why: str = "") -> list:
    """'Remove' physical files by MOVING them into the same disk's .quarantine (a rename: instant, no extra space, fully
    reversible until quarantine_purge.py empties it). Never touches /mnt/user paths. Returns [(path, outcome)]."""
    out = []
    for p in paths:
        if p.startswith(USER_PREFIX) or any(p == f"{MNT}/{v}" or p.startswith(f"{MNT}/{v}/") for v in FUSE_VIEWS) or re.search(r"/(user|user0)/Media/", p):
            out.append((p, "refused: FUSE path")); continue
        if os.path.islink(p) or not os.path.isfile(p):
            out.append((p, "missing")); continue
        if not apply:
            out.append((p, "would quarantine")); continue
        try:
            dest = quarantine_path(p)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.rename(p, dest)
        except (OSError, ValueError) as e:
            _log(logfile, f"QUARANTINE FAILED {p}: {e}")
            out.append((p, f"failed: {e}")); continue
        _log(logfile, f"QUARANTINE {p} -> {dest} ({why})")
        out.append((p, "quarantined"))
    return out


def prune_empty_parents(removed_paths: list, apply: bool, stop_at=("Movies", "tv"), logfile: str = "") -> list:
    """After removing files, rmdir (never rm -r) the now-empty parent folders of exactly those files, walking up but
    stopping at a library root (.../Media/Movies or .../Media/tv) and at the first non-empty folder. Folders that
    were already empty before (e.g. Radarr's placeholder folders for movies still waiting on a download) are never
    touched, because only parents of just-removed files are considered. Only folders INSIDE the Movies or tv trees
    are ever candidates: Media itself, arr/ (qBittorrent's save path) and anything else are left alone."""
    pruned = []
    for p in removed_paths:
        d = os.path.dirname(p)
        while d and os.path.basename(d) not in stop_at and ("/Media/Movies/" in d + "/" or "/Media/tv/" in d + "/"):
            try:
                if os.listdir(d):
                    break
                if apply:
                    os.rmdir(d)
                    _log(logfile, f"RMDIR {d} (emptied by the removal)")
                pruned.append(d)
            except OSError:
                break
            d = os.path.dirname(d)
    return pruned


def qbit_tag(session, base_url: str, hash_: str, apply: bool, logfile: str = "", tag: str = TAG_DELETE_LATER) -> str:
    if not apply:
        return "would tag"
    r = session.post(f"{base_url}/api/v2/torrents/addTags", data={"hashes": hash_, "tags": tag}, timeout=30)
    _log(logfile, f"TAG {tag} {hash_} -> HTTP {r.status_code}")
    return "tagged" if r.ok else f"tag failed HTTP {r.status_code}"


def qbit_delete(session, base_url: str, hash_: str, apply: bool, logfile: str = "", delete_files: bool = False) -> str:
    if not apply:
        return "would delete torrent" + ("+files" if delete_files else " (files kept)")
    r = session.post(f"{base_url}/api/v2/torrents/delete",
                     data={"hashes": hash_, "deleteFiles": "true" if delete_files else "false"}, timeout=60)
    _log(logfile, f"QBIT-DELETE {hash_} deleteFiles={delete_files} -> HTTP {r.status_code}")
    return "deleted" if r.ok else f"delete failed HTTP {r.status_code}"


def execute(decision: Decision, session, base_url: str, apply: bool, logfile: str = "", why: str = "") -> list:
    """Carry out a Decision. Returns human-readable outcomes. Dry-run unless apply=True.
    Order matters: library names are unlinked BEFORE the torrent is removed, never the other way round, and if any of those
    unlinks did not actually happen (file gone, FUSE path refused) the torrent is left completely alone."""
    if decision.mode == "REFUSE":
        return [f"refused: {decision.reason}"]
    outcomes = unlink_raw(decision.unlink_raw, apply, logfile, why or decision.mode)
    res = [f"{o}: {p}" for p, o in outcomes]
    if apply and any(o != "quarantined" for _, o in outcomes):
        res.append("ABORT: a file could not be quarantined, so the torrent was left untouched")
        _log(logfile, f"ABORT {decision.torrent_hash}: unlink outcomes {outcomes}")
        return res
    for label, fn, wanted in (("tag", qbit_tag, decision.tag), ("delete", qbit_delete, decision.delete_torrent)):
        if not wanted:
            continue
        try:
            if label == "delete":
                res.append("qBit " + qbit_delete(session, base_url, decision.torrent_hash, apply, logfile, delete_files=decision.delete_files))
            else:
                res.append("qBit " + fn(session, base_url, decision.torrent_hash, apply, logfile))
        except Exception as e:  # noqa: BLE001 - the library name may already be unlinked; report, do not crash the run
            _log(logfile, f"QBIT-{label.upper()} {decision.torrent_hash} FAILED: {e}")
            res.append(f"qBit {label} FAILED ({type(e).__name__}); library names were already handled - check the torrent by hand")
    return res


def succeeded(lines: list) -> bool:
    """True only if every line of an execute() result is a success or a dry-run note."""
    ok = ("quarantined:", "would", "qBit tagged", "qBit deleted", "qBit would")
    return bool(lines) and all(l.startswith(ok) for l in lines)


def keeper_problem(keeper_raws: list) -> str:
    """Why a keeper cannot be trusted as the surviving copy, or ''. Each must be exactly one physical, regular, non-empty file."""
    for k in keeper_raws:
        if len(k) != 1:
            return "a kept file does not resolve to exactly one physical copy"
        p = k[0]
        if os.path.islink(p) or not os.path.isfile(p) or os.path.getsize(p) == 0:
            return f"the kept file is missing, empty or a symlink: {os.path.basename(p)}"
    return ""


def save_plan(path: str, actions: list):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(actions, f, indent=1)
