#!/usr/bin/env python3
"""
job_runner.py - executes jobs the Plex Dashboard queued, with the same safety layer as every other destructive tool.

The dashboard container is deliberately unprivileged (array mounted read-only). When you ask it to delete a title or
reset a poster it writes a small JSON job; a User Script on the host runs this file to carry it out. Nothing here trusts
the job's numbers: every removal is re-classified against the CURRENT state with safe_ops.classify_removal, and an
`apply` job only runs if the decisions still match the plan you confirmed (plan_digest).

Job files:  JOBS_DIR/pending/<id>.json   -> processed -> JOBS_DIR/done/<id>.json (result added)
  {"id": "...", "type": "delete_files"|"poster_reset", "apply": false, "plan_digest": "", "created": epoch, ...type fields}
  delete_files:  app ("radarr"|"sonarr"), arr_id, title, paths [/media/... file paths Radarr/Sonarr track], remove_record (bool)
  poster_reset:  title
A dry-run job (apply=false) returns the plan in "lines" and a "digest". The matching apply job must carry that digest.

Usage:  python3 job_runner.py             # process everything pending
"""
import glob
import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace

import requests

from common import to_host_path
import safe_ops
from safe_ops import classify_removal, raw_copies

JOBS = os.environ.get("JOBS_DIR", "/jobs")
LOGFILE = "/logs/job-runner.log"
MAX_AGE_S = 30 * 60
KOMETA_DIR = os.environ.get("KOMETA_DIR", "/kometa")
RADARR_URL, RADARR_KEY = os.environ.get("RADARR_URL", ""), os.environ.get("RADARR_KEY", "")
SONARR_URL, SONARR_KEY = os.environ.get("SONARR_URL", ""), os.environ.get("SONARR_KEY", "")
PLEX_URL, PLEX_TOKEN = os.environ.get("PLEX_URL", ""), os.environ.get("PLEX_TOKEN", "")
LIB_ROOTS = ("/mnt/user/Media/Movies/", "/mnt/user/Media/tv/")


def log(msg):
    os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
    with open(LOGFILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def digest_of(items):
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# delete_files
# ---------------------------------------------------------------------------

def plan_delete(job, dr):
    """-> (decisions, lines). decisions: [(host_path, Decision)]. Never touches anything."""
    decisions, lines = [], []
    for p in job["paths"]:
        host, mapped = to_host_path(p)
        if not mapped or not host.startswith(LIB_ROOTS) or os.path.normpath(host) != host:
            decisions.append((host, safe_ops.Decision("REFUSE", "not inside the Movies or tv library")))
            continue
        raw = raw_copies(host)
        nl = safe_ops.file_identity(raw[0])[2] if len(raw) == 1 and os.path.exists(raw[0]) else 0
        r = SimpleNamespace(path=host, raw=raw, nlink=nl)
        decisions.append((host, classify_removal(raw, dr.torrent_for(r))))
    for host, d in decisions:
        lines.append(f"{d.mode:26s} {os.path.basename(host)}" + (f"  [{d.torrent_name[:50]}]" if d.torrent_name else "") + ("" if d.mode != "REFUSE" else f"  ({d.reason})"))
    return decisions, lines


def run_delete(job, dr):
    dr.APPLY = bool(job["apply"])
    decisions, lines = plan_delete(job, dr)
    digest = digest_of([(h, d.mode, d.torrent_hash) for h, d in decisions])
    res = {"digest": digest, "lines": lines, "ok": True}
    if not job["apply"]:
        refused = [h for h, d in decisions if d.mode == "REFUSE"]
        kept = [h for h, d in decisions if d.mode in ("TAG_ONLY", "LIBRARY_NAME_THEN_TAG")]
        res["lines"] += [""] + ([f"{len(refused)} file(s) refused - they will NOT be touched and the record stays."] if refused else []) \
            + ([f"{len(kept)} file(s) belong to young seeds: kept seeding and tagged delete-later (record kept, unmonitored)."] if kept else [])
        res["removes_record"] = bool(job.get("remove_record")) and not refused and not kept
        return res
    if job.get("plan_digest") != digest:
        return {"ok": False, "lines": ["State changed since you confirmed the plan. Nothing was done - plan again."], "digest": digest}
    done_all = True
    for host, d in decisions:
        if d.mode == "REFUSE":
            res["lines"].append(f"{os.path.basename(host)}: refused ({d.reason})")
            done_all = False
            continue
        out = safe_ops.execute(d, dr.QBIT_SESSION, dr.QBIT_URL, apply=True, logfile=LOGFILE, why=f"dashboard delete {job['title']}")
        res["lines"] += [f"{os.path.basename(host)}: {o}" for o in out]
        if d.mode in ("REFUSE", "TAG_ONLY", "LIBRARY_NAME_THEN_TAG"):
            done_all = False
    safe_ops.prune_empty_parents([x for _, d in decisions for x in d.unlink_raw], logfile=LOGFILE)
    base, key = (RADARR_URL, RADARR_KEY) if job["app"] == "radarr" else (SONARR_URL, SONARR_KEY)
    kind = "movie" if job["app"] == "radarr" else "series"
    h = {"X-Api-Key": key}
    if job.get("remove_record") and done_all:
        r = requests.delete(f"{base}/api/v3/{kind}/{job['arr_id']}", params={"deleteFiles": "false", ("addImportExclusion" if job["app"] == "radarr" else "addImportListExclusion"): "true"}, headers=h, timeout=60)
        res["lines"].append(f"{job['app']} record removed (HTTP {r.status_code})")
        res["ok"] = r.ok
    else:
        try:
            g = requests.get(f"{base}/api/v3/{kind}/{job['arr_id']}", headers=h, timeout=60)
            g.raise_for_status()
            body = g.json()
            body["monitored"] = False
            requests.put(f"{base}/api/v3/{kind}/{job['arr_id']}", headers=h, json=body, timeout=60).raise_for_status()
            res["lines"].append(f"{job['app']} record kept and unmonitored" + ("" if done_all else " (some files were kept or refused)"))
        except requests.RequestException as e:
            res["lines"].append(f"could not unmonitor in {job['app']}: {e}")
            res["ok"] = False
    if PLEX_TOKEN:
        dr.plex_refresh_and_empty_trash()
        res["lines"].append("Plex scanned and trash emptied")
    log(f"DELETE job {job['id']} {job['title']}: {res['lines']}")
    return res


# ---------------------------------------------------------------------------
# poster_reset  (same effect as kometa/repick-poster.sh: forget Kometa's saved original and its Overlay label)
# ---------------------------------------------------------------------------

def run_poster_reset(job):
    h = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"}
    r = requests.get(f"{PLEX_URL}/library/sections/1/all", params={"type": 1, "title": job["title"]}, headers=h, timeout=60)
    r.raise_for_status()
    items = [m for m in r.json()["MediaContainer"].get("Metadata", []) if m["title"] == job["title"]]
    lines = []
    for m in items:
        key = m["ratingKey"]
        backups = glob.glob(os.path.join(KOMETA_DIR, "overlays", "Movies Original Posters", f"{key}.*"))
        lines.append(f"{m['title']} ({m.get('year')}) ratingKey={key}: backup {[os.path.basename(b) for b in backups] or 'none'}, remove Overlay label")
        if job["apply"]:
            for b in backups:
                os.remove(b)
            requests.put(f"{PLEX_URL}/library/sections/1/all", params={"type": 1, "id": key, "label[].tag.tag-": "Overlay"}, headers=h, timeout=60).raise_for_status()
            log(f"POSTER-RESET {m['title']} {key}")
    if not items:
        return {"ok": False, "lines": [f"no movie titled '{job['title']}' in Plex"]}
    return {"ok": True, "lines": lines + ([] if job["apply"] else ["Kometa will redraw the overlay from the poster Plex currently shows on its next run."])}


# ---------------------------------------------------------------------------

def process(path, dr):
    with open(path) as f:
        job = json.load(f)
    jid = job.get("id") or os.path.basename(path)[:-5]
    if time.time() - job.get("created", 0) > MAX_AGE_S:
        result = {"ok": False, "lines": ["job expired (older than 30 minutes); nothing was done"]}
    else:
        try:
            if job["type"] == "delete_files":
                result = run_delete(job, dr)
            elif job["type"] == "poster_reset":
                result = run_poster_reset(job)
            else:
                result = {"ok": False, "lines": [f"unknown job type {job['type']!r}"]}
        except Exception as e:  # noqa: BLE001 - always write a result so the page does not wait forever
            log(f"job {jid} failed: {e!r}")
            result = {"ok": False, "lines": [f"failed: {type(e).__name__}: {e}"]}
    job["result"], job["finished"] = result, time.time()
    os.makedirs(os.path.join(JOBS, "done"), exist_ok=True)
    with open(os.path.join(JOBS, "done", f"{jid}.json"), "w") as f:
        json.dump(job, f)
    os.remove(path)


def main():
    pending = sorted(glob.glob(os.path.join(JOBS, "pending", "*.json")))
    if not pending:
        return
    sys.argv = [sys.argv[0]]            # dupe_review reads --apply from argv; APPLY is set per job instead
    import dupe_review as dr
    dr.load_qbit_index()
    for p in pending:
        job = json.load(open(p))
        if dr.QBIT_SESSION is None and job.get("type") == "delete_files":
            # fail closed: no seeding check, no deletes
            job["result"] = {"ok": False, "lines": ["qBittorrent unreachable: refusing to delete anything"]}
            os.makedirs(os.path.join(JOBS, "done"), exist_ok=True)
            with open(os.path.join(JOBS, "done", os.path.basename(p)), "w") as f:
                json.dump(job, f)
            os.remove(p)
            continue
        process(p, dr)


if __name__ == "__main__":
    main()
