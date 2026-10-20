#!/usr/bin/env python3
"""
hnr_guard.py - stop Radarr/Sonarr from upgrading (and so deleting) a library file that is still a young seed.

Why: Radarr/Sonarr delete the old file when an upgrade imports. If that old library file is the very
file a torrent is seeding (torrent save_path inside the library, not in arr/ where the library name is
only a hardlink), the delete kills the seed. Inside a tracker's hit-and-run window that is a violation.

What it does (dry-run unless --apply):
  * Finds qBittorrent torrents whose data lives in the library (save_path not under /arr) and that have
    seeded for fewer than HNR_MIN_SEED_DAYS (default 30), or whose name is listed in hnr_extra.txt
    (one substring per line; use it for torrents a tracker shows as "unsatisfied").
  * Maps each to the Radarr movie / Sonarr episode that owns the file and sets it unmonitored.
  * Records what it held in logs/hnr_guard_state.json so it can re-monitor when the window has passed
    (or the torrent is gone). Items you unmonitored yourself are never touched: only held ones are released.

Usage:  python3 hnr_guard.py            # dry-run: show what would be held / released
        python3 hnr_guard.py --apply    # do it
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

HNR_DAYS = float(os.environ.get("HNR_MIN_SEED_DAYS", "30"))
STATE = "/logs/hnr_guard_state.json"
EXTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hnr_extra.txt")


def _arr(app):
    url, key = (RADARR_URL, RADARR_KEY) if app == "radarr" else (SONARR_URL, SONARR_KEY)
    return f"{url}/api/v3", {"X-Api-Key": key}


def arr_get(app, path):
    base, h = _arr(app)
    r = requests.get(f"{base}/{path}", headers=h, timeout=120)
    r.raise_for_status()
    return r.json()


def arr_put(app, path, body):
    base, h = _arr(app)
    r = requests.put(f"{base}/{path}", headers=h, json=body, timeout=120)
    r.raise_for_status()


def qbit_session():
    s = requests.Session()
    r = s.post(f"{QBIT_URL}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=30)
    if r.text.strip() != "Ok.":
        sys.exit("qBittorrent login failed")
    return s


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"radarr": {}, "sonarr": {}}


def main():
    apply = "--apply" in sys.argv
    q = qbit_session()
    base = QBIT_URL
    torrents = q.get(f"{base}/api/v2/torrents/info", timeout=60).json()
    extra = []
    if os.path.exists(EXTRA):
        extra = [l.strip().lower() for l in open(EXTRA) if l.strip() and not l.startswith("#")]
    now = time.time()
    young = {}  # torrent hash -> (name, set of file basenames)
    for t in torrents:
        if t["save_path"].startswith("/arr"):
            continue  # library name is only a hardlink of the arr/ seed: safe to delete
        age = (now - t["added_on"]) / 86400
        listed = any(x in t["name"].lower() for x in extra)
        if age < HNR_DAYS or listed:
            files = q.get(f"{base}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=60).json()
            young[t["hash"]] = (t["name"], {os.path.basename(f["name"]) for f in files}, age, listed)
    held_names = set().union(*[v[1] for v in young.values()]) if young else set()
    state = load_state()
    plan = []

    # Radarr
    radarr = arr_get("radarr", "movie")
    for m in radarr:
        mf = m.get("movieFile")
        if mf and os.path.basename(mf["relativePath"]) in held_names and m["monitored"]:
            plan.append(("radarr", m["id"], m["title"], "hold"))
    for mid, info in list(state["radarr"].items()):
        if not any(h in young for h in info["hashes"]):
            plan.append(("radarr", int(mid), info["title"], "release"))
    # Sonarr
    for s in arr_get("sonarr", "series"):
        if not s.get("statistics", {}).get("episodeFileCount"):
            continue
        for ef in arr_get("sonarr", f"episodefile?seriesId={s['id']}"):
            if os.path.basename(ef["relativePath"]) in held_names:
                for ep in arr_get("sonarr", f"episode?episodeFileId={ef['id']}"):
                    if ep["monitored"]:
                        plan.append(("sonarr", ep["id"], f"{s['title']} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}", "hold"))
    for eid, info in list(state["sonarr"].items()):
        if not any(h in young for h in info["hashes"]):
            plan.append(("sonarr", int(eid), info["title"], "release"))

    for app, ident, title, act in plan:
        print(f"{'APPLY' if apply else 'DRY'} {act:7s} {app} {ident} {title}")
    if not plan:
        print("nothing to hold or release")
        return
    if not apply:
        return
    for app, ident, title, act in plan:
        want = (act == "release")
        if app == "radarr":
            m = arr_get("radarr", f"movie/{ident}")
            m["monitored"] = want
            arr_put("radarr", f"movie/{ident}", m)
        else:
            arr_put("sonarr", "episode/monitor", {"episodeIds": [ident], "monitored": want})
        key = str(ident)
        if act == "hold":
            state[app][key] = {"title": title, "hashes": list(young.keys())}
        else:
            state[app].pop(key, None)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    main()
