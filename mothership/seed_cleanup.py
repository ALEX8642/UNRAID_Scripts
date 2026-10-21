#!/usr/bin/env python3
"""
seed_cleanup.py - remove qBittorrent torrents whose data was deleted on purpose by a Radarr/Sonarr UPGRADE.

When Radarr/Sonarr import a better release they delete the old library file. If that file was the data of an old library-owned seed, the torrent goes to
"missingFiles" and stays there forever. This removes such torrents, but only when EVERY safety check passes (dry-run unless --apply):

  1. qBittorrent reports the torrent as missingFiles, AND its content is absent on every raw array disk (/mnt/disk*, /mnt/cache) - not just through the share.
  2. Radarr (movies) or Sonarr (episodes) history shows a file with the same name deleted with reason "Upgrade"  -> the deletion was intentional, not an accident.
  3. The tracker's seeding rule was met: completed at least HNR_MIN_SEED_DAYS ago (default 5 = BHD's 120 h) or ratio >= 1. Otherwise it is only reported.
  4. Two-strike rule: the torrent must have been seen in this state on a previous run at least 20 hours earlier (state file), so a transient mount problem cannot trigger removal.
  5. Circuit breaker: if more than MAX_PER_RUN (default 5) torrents qualify at once, nothing is removed (that pattern means something else broke, e.g. a mount or a relocation).
Removal uses deleteFiles=false (the data is already gone) and a log line per torrent. Needs /mnt mounted read-only into the container.

Usage:  python3 seed_cleanup.py           # dry run
        python3 seed_cleanup.py --apply   # remove
"""
import json
import os
import sys
import time

import requests

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


def raw_candidates(qpath):
    for pre, rel in PATH_MAP.items():
        if qpath == pre or qpath.startswith(pre + "/"):
            sub = qpath[len(pre):].lstrip("/")
            out = []
            for base in sorted(os.listdir("/mnt")):
                if base.startswith("disk") or base == "cache":
                    out.append(os.path.join("/mnt", base, rel, sub))
            return out
    return None


def exists_on_array(qpath):
    c = raw_candidates(qpath)
    if c is None:
        return True  # unknown location: treat as present, never remove
    return any(os.path.exists(p) for p in c)


def upgrade_deleted_names(app):
    """Set of base names of files the app deleted because of an Upgrade."""
    url, key = (RADARR_URL, RADARR_KEY) if app == "radarr" else (SONARR_URL, SONARR_KEY)
    et = "movieFileDeleted" if app == "radarr" else "episodeFileDeleted"
    names, page = set(), 1
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
                names.add(os.path.basename(h.get("sourceTitle", "")))
        page += 1
    return names


def torrent_main_names(q, t):
    files = q.get(f"{QBIT_URL}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=60).json()
    big = sorted(files, key=lambda f: -f["size"])[:3]
    return {os.path.basename(f["name"]) for f in big}


def seed_rule_met(t, now):
    done = t.get("completion_on", 0) or t["added_on"]
    return t.get("ratio", 0) >= 1.0 or (now - done) / 86400 >= HNR_DAYS


def main():
    apply = "--apply" in sys.argv
    if not os.path.isdir("/mnt/user") and not os.path.isdir("/mnt/disk1"):
        sys.exit("array not visible (mount /mnt read-only into the container); refusing to run")
    q = requests.Session()
    if q.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=30).text.strip() != "Ok.":
        sys.exit("qBittorrent login failed")
    torrents = q.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=60).json()
    now = time.time()
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    missing = [t for t in torrents if t["state"] == "missingFiles"]
    print(f"torrents in missingFiles: {len(missing)}")
    deleted = None
    cands, notes = [], []
    for t in missing:
        label = t["name"][:80]
        if exists_on_array(t["content_path"]):
            notes.append(f"KEEP   {label}: content exists on the array (qBittorrent path problem, not a deletion)")
            continue
        if deleted is None:
            deleted = upgrade_deleted_names("radarr") | upgrade_deleted_names("sonarr")
        if not (torrent_main_names(q, t) & deleted):
            notes.append(f"KEEP   {label}: no Radarr/Sonarr upgrade-deletion record for its files")
            continue
        if not seed_rule_met(t, now):
            notes.append(f"WAIT   {label}: seeding rule (120 h or ratio 1.0) not met yet")
            continue
        first = state.get(t["hash"], now)
        state[t["hash"]] = first
        if now - first < STRIKE_HOURS * 3600:
            notes.append(f"WAIT   {label}: first seen missing just now, needs a second sighting 20+ h later")
            continue
        cands.append(t)
    state = {h: v for h, v in state.items() if h in {t["hash"] for t in missing}}
    for t in missing:
        state.setdefault(t["hash"], now)
    for n in notes:
        print(n)
    if len(cands) > MAX_PER_RUN:
        print(f"CIRCUIT BREAKER: {len(cands)} torrents qualify (limit {MAX_PER_RUN}); removing nothing. Investigate first.")
        cands = []
    for t in cands:
        print(f"{'REMOVED' if apply else 'WOULD REMOVE'} {t['name'][:90]} (upgrade deleted its data)")
        if apply:
            q.post(f"{QBIT_URL}/api/v2/torrents/delete", data={"hashes": t["hash"], "deleteFiles": "false"}, timeout=60).raise_for_status()
            state.pop(t["hash"], None)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"))
    if not missing:
        print("nothing to do")


if __name__ == "__main__":
    main()
