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

import glob
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

TAG_DELETE_LATER = "delete-later"
# Conservative stand-in for tracker hit-and-run rules (which are members-only and vary):
# a torrent seeded for fewer days than this is treated as possibly still inside its window.
HNR_MIN_SEED_DAYS = float(os.environ.get("HNR_MIN_SEED_DAYS", "30"))
# Torrents with more than this many files are treated as packs: never deleted per file.
MAX_TORRENT_FILES = 3
# Where the physical array disks live (overridable for tests).
DISK_GLOB = os.environ.get("MOTHERSHIP_DISK_GLOB", "/mnt/disk[0-9]*")
USER_PREFIX = "/mnt/user/"


def raw_copies(host_path: str, disk_glob: Optional[str] = None) -> list:
    """/mnt/user/Media/x -> every physical copy /mnt/diskN/Media/x that exists (plus the cache pool)."""
    disk_glob = disk_glob or DISK_GLOB
    if not host_path.startswith(USER_PREFIX):
        return []
    rel = host_path[len(USER_PREFIX):]
    hits = [p for p in glob.glob(os.path.join(disk_glob, rel)) if os.path.lexists(p)]
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
            out = subprocess.run(["find", d, "-xdev", "-type", "f", "-inum", str(st.st_ino)], capture_output=True, text=True).stdout
            found += [l for l in out.splitlines() if l and l != raw_path]
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


@dataclass
class Decision:
    mode: str                         # see MODES
    reason: str
    unlink_raw: list = field(default_factory=list)   # physical library names to unlink
    delete_torrent: bool = False                      # then remove the torrent + files via qBittorrent's API
    tag: bool = False                                 # keep the torrent seeding, tag it delete-later
    torrent_hash: str = ""
    torrent_name: str = ""

    def as_dict(self):
        return asdict(self)


MODES = {
    "UNLINK": "no torrent owns this file: unlink the physical copy",
    "LIBRARY_NAME_THEN_DELETE": "library name is a hardlink of a seed kept elsewhere (arr/), seeded long enough: unlink the library name, then remove the torrent + files via qBittorrent",
    "LIBRARY_NAME_THEN_TAG": "library name is a hardlink of a seed kept elsewhere (arr/), possibly inside a hit-and-run window: unlink only the Plex-visible name, keep seeding, tag delete-later",
    "QBIT_DELETE": "the torrent's own tracked name, seeded long enough: remove the torrent + files via qBittorrent's API",
    "TAG_ONLY": "the torrent's own tracked name and the window may be open: unlinking would break the seed, so only tag delete-later and leave the files (hide it later by relocating the seed)",
    "REFUSE": "refused for safety",
}


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
    if not torrent.covers_exact_path:
        return Decision("LIBRARY_NAME_THEN_DELETE" if seasoned else "LIBRARY_NAME_THEN_TAG",
                        MODES["LIBRARY_NAME_THEN_DELETE" if seasoned else "LIBRARY_NAME_THEN_TAG"],
                        unlink_raw=list(raw_paths), delete_torrent=seasoned, tag=not seasoned, **base)
    if seasoned:
        return Decision("QBIT_DELETE", MODES["QBIT_DELETE"], delete_torrent=True, **base)
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


def unlink_raw(paths: list, apply: bool, logfile: str = "", why: str = "") -> list:
    """Unlink physical files. Returns list of (path, outcome). Never touches /mnt/user paths."""
    out = []
    for p in paths:
        if p.startswith(USER_PREFIX):
            out.append((p, "refused: FUSE path")); continue
        if not os.path.isfile(p):
            out.append((p, "missing")); continue
        if not apply:
            out.append((p, "would unlink")); continue
        os.remove(p)
        _log(logfile, f"UNLINK {p} ({why})")
        out.append((p, "unlinked"))
    return out


def qbit_tag(session, base_url: str, hash_: str, apply: bool, logfile: str = "", tag: str = TAG_DELETE_LATER) -> str:
    if not apply:
        return "would tag"
    r = session.post(f"{base_url}/api/v2/torrents/addTags", data={"hashes": hash_, "tags": tag}, timeout=30)
    _log(logfile, f"TAG {tag} {hash_} -> HTTP {r.status_code}")
    return "tagged" if r.ok else f"tag failed HTTP {r.status_code}"


def qbit_delete(session, base_url: str, hash_: str, apply: bool, logfile: str = "", delete_files: bool = True) -> str:
    if not apply:
        return "would delete torrent+files"
    r = session.post(f"{base_url}/api/v2/torrents/delete",
                     data={"hashes": hash_, "deleteFiles": "true" if delete_files else "false"}, timeout=60)
    _log(logfile, f"QBIT-DELETE {hash_} deleteFiles={delete_files} -> HTTP {r.status_code}")
    return "deleted" if r.ok else f"delete failed HTTP {r.status_code}"


def execute(decision: Decision, session, base_url: str, apply: bool, logfile: str = "", why: str = "") -> list:
    """Carry out a Decision. Returns human-readable outcomes. Dry-run unless apply=True.
    Order matters: library names are unlinked BEFORE the torrent is removed, never the other way round."""
    if decision.mode == "REFUSE":
        return [f"refused: {decision.reason}"]
    res = [f"{o}: {p}" for p, o in unlink_raw(decision.unlink_raw, apply, logfile, why or decision.mode)]
    if decision.tag:
        res.append("qBit " + qbit_tag(session, base_url, decision.torrent_hash, apply, logfile))
    if decision.delete_torrent:
        res.append("qBit " + qbit_delete(session, base_url, decision.torrent_hash, apply, logfile))
    return res


def save_plan(path: str, actions: list):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(actions, f, indent=1)
