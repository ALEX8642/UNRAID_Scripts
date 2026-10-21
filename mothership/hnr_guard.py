#!/usr/bin/env python3
"""
hnr_guard.py - stop Radarr/Sonarr from upgrading (and so deleting) a library file that is still a young seed.

Why: Radarr/Sonarr delete the old file when an upgrade imports. If that old library file is the very
file a torrent is seeding (torrent save_path inside the library, not in arr/ where the library name is
only a hardlink), the delete kills the seed. Inside a tracker's hit-and-run window that is a violation.

What it does (dry-run unless --apply):
  * Finds qBittorrent torrents whose data lives in the library (save_path not under /arr) and that have not met the
    tracker rule (safe_ops.seed_rule_met: ratio 1.0, 120 h of real seeding, or a long-active seed), or whose name is
    listed in hnr_extra.txt (one substring per line; use it for torrents a tracker shows as "unsatisfied").
  * Maps each to the Radarr movie / Sonarr episode that owns the file and sets it unmonitored.
  * Records WHICH torrent holds each item in logs/hnr_guard_state.json. An item is released only when every one of its
    torrents is present and has met the rule, or has been absent from qBittorrent on two consecutive runs. Items you
    unmonitored yourself are never touched: only held ones are released.

Fail-safe rules: it never acts on an empty or suspiciously small torrent list (qBittorrent restarting), an empty Radarr
list while holds exist, or a corrupt state file; the state is written atomically after every single change; two
instances cannot run at once; one failing item never stops the others, and any failure makes the exit code non-zero.

Usage:  python3 hnr_guard.py            # dry-run: show what would be held / released
        python3 hnr_guard.py --apply    # do it
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

HNR_DAYS = max(float(os.environ.get("HNR_MIN_SEED_DAYS", "5")), safe_ops.BHD_RULE_DAYS)  # BHD: 120 h seeding OR ratio 1.0; never lower
STATE = os.environ.get("HNR_STATE", "/logs/hnr_guard_state.json")
LOCK = os.environ.get("HNR_LOCK", "/logs/hnr_guard.lock")
EXTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hnr_extra.txt")
MIN_TORRENT_FRACTION = 0.6   # fewer torrents than this share of the previous run means qBittorrent has not finished loading


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
    """A state file that exists but cannot be parsed is an error: guessing 'no holds' would orphan every held item."""
    if not os.path.exists(STATE):
        return {"radarr": {}, "sonarr": {}, "_n": 0}
    with open(STATE) as f:
        st = json.load(f)
    st.setdefault("radarr", {})
    st.setdefault("sonarr", {})
    st.setdefault("_n", 0)
    return st


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)


def release_ok(entry, torrents_by_hash, young_hashes):
    """Release a hold only on positive evidence. Returns (release: bool, new_absent_count)."""
    hashes = entry.get("hashes") or []
    if not hashes:
        return False, entry.get("absent", 0)          # nothing recorded about who holds it: never release blindly
    present = [h for h in hashes if h in torrents_by_hash]
    if len(present) < len(hashes):
        absent = entry.get("absent", 0) + 1
        return absent >= 2 and not any(h in young_hashes for h in present), absent
    return not any(h in young_hashes for h in present), 0


def main():
    apply = "--apply" in sys.argv
    try:
        lock = open(LOCK, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another hnr_guard run is in progress")
    except OSError:
        lock = None
    q = qbit_session()
    base = QBIT_URL
    torrents = q.get(f"{base}/api/v2/torrents/info", timeout=60).json()
    state = load_state()
    if not torrents:
        # qBittorrent restarting or an API hiccup: with no torrents everything held would look "released" and get
        # re-monitored, letting Radarr/Sonarr upgrade (delete) young seeds. Never act on an empty answer.
        sys.exit("qBittorrent returned no torrents; refusing to hold or release anything")
    if state["_n"] and len(torrents) < MIN_TORRENT_FRACTION * state["_n"]:
        sys.exit(f"qBittorrent lists {len(torrents)} torrents but {state['_n']} were seen last time (still loading?); refusing to act")
    extra = []
    if os.path.exists(EXTRA):
        with open(EXTRA) as f:
            extra = [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]
    now = time.time()
    by_hash = {t["hash"]: t for t in torrents}
    young = {}   # torrent hash -> (name, set of file basenames)
    for t in torrents:
        if t["save_path"] == "/arr" or t["save_path"].startswith("/arr/"):
            continue  # library name is only a hardlink of the arr/ seed: safe to delete
        listed = any(x in t["name"].lower() for x in extra)
        if listed or not safe_ops.seed_rule_met(t, now, HNR_DAYS):
            files = q.get(f"{base}/api/v2/torrents/files", params={"hash": t["hash"]}, timeout=60).json()
            young[t["hash"]] = (t["name"], {os.path.basename(f["name"]) for f in files})
    held_names = set().union(*[v[1] for v in young.values()]) if young else set()
    owners = {}  # basename -> [young hashes owning it]
    for h, (_n, names) in young.items():
        for n in names:
            owners.setdefault(n, []).append(h)
    plan, errors = [], 0

    # ---- Radarr
    radarr = arr_get("radarr", "movie")
    if not radarr and state["radarr"]:
        sys.exit("Radarr returned no movies while holds exist; refusing to act")
    by_id = {str(m["id"]): m for m in radarr}
    for m in radarr:
        mf = m.get("movieFile")
        if mf and os.path.basename(mf["relativePath"]) in held_names and m["monitored"]:
            plan.append(("radarr", m["id"], m["title"], "hold", owners[os.path.basename(mf["relativePath"])]))
    for mid, info in list(state["radarr"].items()):
        if mid not in by_id:
            plan.append(("radarr", int(mid), info["title"], "forget", []))      # the movie no longer exists
            continue
        rel, absent = release_ok(info, by_hash, set(young))
        if absent != info.get("absent", 0):
            info["absent"] = absent
        if rel:
            plan.append(("radarr", int(mid), info["title"], "release", []))

    # ---- Sonarr: each read is guarded; one bad series must not disable protection for everything else
    try:
        series = arr_get("sonarr", "series")
    except requests.RequestException as e:
        print(f"FAILED reading Sonarr series: {e}")
        series, errors = [], errors + 1
    for s in series:
        if not s.get("statistics", {}).get("episodeFileCount"):
            continue
        try:
            for ef in arr_get("sonarr", f"episodefile?seriesId={s['id']}"):
                n = os.path.basename(ef["relativePath"])
                if n in held_names:
                    for ep in arr_get("sonarr", f"episode?episodeFileId={ef['id']}"):
                        if ep["monitored"]:
                            plan.append(("sonarr", ep["id"], f"{s['title']} S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}", "hold", owners[n]))
        except requests.RequestException as e:
            print(f"FAILED reading Sonarr series {s.get('title')}: {e}")
            errors += 1
    for eid, info in list(state["sonarr"].items()):
        rel, absent = release_ok(info, by_hash, set(young))
        if absent != info.get("absent", 0):
            info["absent"] = absent
        if rel:
            plan.append(("sonarr", int(eid), info["title"], "release", []))

    for app, ident, title, act, hs in plan:
        print(f"{'APPLY' if apply else 'DRY'} {act:7s} {app} {ident} {title}")
    if not plan:
        print("nothing to hold or release")
    if not apply:
        return 1 if errors else 0
    state["_n"] = len(torrents)
    save_state(state)
    for app, ident, title, act, hs in plan:
        key = str(ident)
        if act == "forget":
            state[app].pop(key, None)
            save_state(state)
            continue
        want = (act == "release")
        if act == "hold":
            state[app][key] = {"title": title, "hashes": sorted(hs), "absent": 0}
            save_state(state)               # intent first: a crash after the PUT must still leave the hold on record
        try:
            if app == "radarr":
                m = arr_get("radarr", f"movie/{ident}")
                m["monitored"] = want
                arr_put("radarr", f"movie/{ident}", m)
            else:
                arr_put("sonarr", "episode/monitor", {"episodeIds": [ident], "monitored": want})
        except requests.RequestException as e:
            # One bad item (e.g. a movie deleted from Radarr) must not stop the rest.
            print(f"FAILED {act} {app} {ident} {title}: {e}")
            errors += 1
            if act == "hold":
                state[app].pop(key, None)    # the unmonitor did not happen, so there is nothing to release later
            elif getattr(getattr(e, "response", None), "status_code", None) == 404:
                state[app].pop(key, None)    # the item no longer exists: nothing left to release
            save_state(state)
            continue
        if act == "release":
            state[app].pop(key, None)
            save_state(state)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
