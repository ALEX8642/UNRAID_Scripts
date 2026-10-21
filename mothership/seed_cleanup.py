#!/usr/bin/env python3
"""
seed_cleanup.py - remove qBittorrent torrents whose data was deleted on purpose by a Radarr/Sonarr UPGRADE.

When Radarr/Sonarr import a better release they delete the old library file. If that file was the data of an old library-owned seed, the torrent goes to
"missingFiles" and stays there forever. This removes such torrents, but only when EVERY safety check passes (dry-run unless --apply):

  1. qBittorrent reports the torrent as missingFiles, AND its content is absent on every raw array disk (/mnt/disk*, /mnt/cache) - not just through the share.
  2. Radarr (movies) or Sonarr (episodes) history shows a file with the same name deleted with reason "Upgrade"  -> the deletion was intentional, not an accident.
  3. The tracker's seeding rule was really met (safe_ops.seed_rule_met): ratio >= 1, or 120 h of seeding_time, or - because fastresume edits reset
     seeding_time - an active seed older than 5 days / any torrent older than 14 days. A never-completed torrent (completion_on -1) is never 'met'.
     The upgrade evidence must also be dated AFTER the torrent completed (a stale record for a same-named file proves nothing).
  4. Two-strike rule: the torrent must have passed every check above on a previous APPLY run at least 20 hours earlier (state file, written atomically,
     never by a dry run), so a transient mount problem cannot trigger removal.
  5. Circuit breaker: if more than MAX_PER_RUN (default 5) torrents qualify at once, nothing is removed (that pattern means something else broke, e.g. a mount or a relocation).
Removal uses deleteFiles=false (the data is already gone) and a log line per torrent. Needs /mnt mounted read-only into the container.

Usage:  python3 seed_cleanup.py           # dry run
        python3 seed_cleanup.py --apply   # remove
"""
import fcntl
import json
import os
import sys
import time

import requests

import safe_ops

RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8080")
QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
HNR_DAYS = float(os.environ.get("HNR_MIN_SEED_DAYS", "5"))
MAX_PER_RUN = int(os.environ.get("SEED_CLEANUP_MAX", "5"))
STRIKE_HOURS = 20
STATE = "/logs/seed_cleanup_state.json"
# qBittorrent container path prefix -> folder under Media on the array
PATH_MAP = {"/movies": "Media/Movies", "/tv": "Media/tv", "/arr": "Media/arr"}


PATH_MAP = {"/movies": "Media/Movies", "/tv": "Media/tv", "/arr": "Media/arr"}
NOT_POOLS = ("user", "user0", "disks", "remotes", "addons", "rootshare")


def raw_candidates(qpath):
    for pre, rel in PATH_MAP.items():
        if qpath == pre or qpath.startswith(pre + "/"):
            sub = qpath[len(pre):].lstrip("/")
            out = [os.path.join("/mnt", "user", rel, sub)]      # the share sees every disk and every pool, including ones we do not list
            for base in sorted(os.listdir("/mnt")):
                if base not in NOT_POOLS and os.path.isdir(os.path.join("/mnt", base)):
                    out.append(os.path.join("/mnt", base, rel, sub))
            return out
    return None


def exists_on_array(qpath):
    c = raw_candidates(qpath)
    if c is None:
        return True  # unknown location: treat as present, never remove
    return any(os.path.exists(p) for p in c)


def _ts(s):
    import datetime
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def upgrade_deleted_names(app):
    """{base name: EARLIEST epoch} of files the app deleted because of an Upgrade."""
    url, key = (RADARR_URL, RADARR_KEY) if app == "radarr" else (SONARR_URL, SONARR_KEY)
    names, page = {}, 1
    while page <= 20:
        r = requests.get(f"{url}/api/v3/history", params={"page": page, "pageSize": 500, "eventType": 6 if app == "radarr" else 5,
                                                          "sortKey": "date", "sortDirection": "descending"},
                         headers={"X-Api-Key": key}, timeout=120)
        r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            break
        for h in recs:
            if (h.get("data") or {}).get("reason", "").lower() == "upgrade":
                n = os.path.basename(h.get("sourceTitle", ""))
                t = _ts(h.get("date", ""))
                names[n] = min(names.get(n, t), t)
        page += 1
    return names


def torrent_main_names(q, t):
    files = q.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=60).json()
    big = sorted(files, key=lambda f: -f["size"])[:3]
    return {os.path.basename(f["name"]) for f in big}


def content_present(q, t):
    """True if any of the torrent's own files still exists on the array. A bare directory check is not enough: after an
    upgrade the library folder can still exist (holding the NEW files) while this torrent's files are gone."""
    files = q.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=60).json()
    sp = t.get("save_path", "").rstrip("/")
    if not files:
        return exists_on_array(t["content_path"])
    big = [f for f in sorted(files, key=lambda f: -f["size"])[:3] if f["size"] > 50 * 1024 * 1024] or files[:1]  # the video(s); leftover .nfo/.jpg do not count as content
    return any(exists_on_array(f"{sp}/{f['name']}") for f in big)


def seed_rule_met(t, now):
    return safe_ops.seed_rule_met(t, now, HNR_DAYS)


def load_state():
    """-> dict. A state file that exists but cannot be read is an error, not 'no history': refuse to guess."""
    if not os.path.exists(STATE):
        return {}
    with open(STATE) as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def mounted_disks():
    return sum(1 for d in os.listdir("/mnt") if d.startswith("disk") and d[4:].isdigit() and os.path.ismount(os.path.join("/mnt", d)))


def main():
    apply = "--apply" in sys.argv
    if not os.path.isdir("/mnt/user") and not os.path.isdir("/mnt/disk1"):
        sys.exit("array not visible (mount /mnt read-only into the container); refusing to run")
    try:
        lock = open(os.environ.get("SEED_CLEANUP_LOCK", "/logs/seed_cleanup.lock"), "w")
    except OSError:
        lock = None
    if lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("another seed_cleanup run is in progress")
    q = requests.Session()
    if q.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=30).text.strip() != "Ok.":
        sys.exit("qBittorrent login failed")
    torrents = q.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=60).json()
    now = time.time()
    state = load_state()
    disks = mounted_disks()
    if disks and state.get("_disks") and disks < state["_disks"]:
        sys.exit(f"only {disks} data disks are mounted but {state['_disks']} were seen before: a missing disk would make its files look deleted. Refusing.")
    missing = [t for t in torrents if t["state"] == "missingFiles"]
    print(f"torrents in missingFiles: {len(missing)}")
    deleted = None
    cands, notes = [], []
    seen = {k: v for k, v in state.items() if not k.startswith("_")}
    new_seen = {}
    for t in missing:
        label = t["name"][:80]
        if content_present(q, t):
            notes.append(f"KEEP   {label}: content exists on the array (qBittorrent path problem, not a deletion)")
            continue
        if deleted is None:
            deleted = {**upgrade_deleted_names("radarr"), **upgrade_deleted_names("sonarr")}
        when = [deleted[n] for n in torrent_main_names(q, t) if n in deleted]
        if not when:
            notes.append(f"KEEP   {label}: no Radarr/Sonarr upgrade-deletion record for its files")
            continue
        done = t.get("completion_on") or 0
        if done > 0 and max(when) < done - 3600:
            notes.append(f"KEEP   {label}: the upgrade record predates this torrent's download; it proves nothing about this copy")
            continue
        if not seed_rule_met(t, now):
            notes.append(f"WAIT   {label}: seeding rule (120 h seeded, ratio 1.0, or 14 days) not met yet")
            continue
        first = seen.get(t["hash"], now)
        new_seen[t["hash"]] = first          # the strike clock starts when EVERY check passes, not at the first sighting
        if now - first < STRIKE_HOURS * 3600:
            notes.append(f"WAIT   {label}: all checks pass; needs a second sighting 20+ h after the first (apply runs only)")
            continue
        cands.append(t)
    for n in notes:
        print(n)
    if len(cands) > MAX_PER_RUN:
        print(f"CIRCUIT BREAKER: {len(cands)} torrents qualify (limit {MAX_PER_RUN}); removing nothing. Investigate first.")
        cands = []
    if apply:
        save_state({**new_seen, "_disks": max(disks, state.get("_disks", 0))})      # persist BEFORE deleting anything
    for t in cands:
        print(f"{'REMOVED' if apply else 'WOULD REMOVE'} {t['name'][:90]} (upgrade deleted its data)")
        if apply:
            q.post(f"{QBIT_URL}/api/v2/torrents/delete", data={"hashes": t["hash"], "deleteFiles": "false"}, timeout=60).raise_for_status()
            new_seen.pop(t["hash"], None)
    if apply and cands:
        save_state({**new_seen, "_disks": max(disks, state.get("_disks", 0))})
    if not missing:
        print("nothing to do")


if __name__ == "__main__":
    main()
