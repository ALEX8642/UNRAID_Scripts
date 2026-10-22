"""Hostile-review tests for job_runner.py. Every test here is expected to FAIL on the current code (each demonstrates a defect)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOW = time.time()


class Fake:
    def __init__(self):
        self.state = {"torrents": [], "files": {}, "movie": {}, "calls": [], "logins": 0, "series_files": [], "info_calls": 0,
                      "info_delay": 0, "drop_delete": False, "on_login": None, "on_info": None}
        st = self.state

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, code=200, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n).decode()
                q = {k: v[0] for k, v in parse_qs(raw).items()}
                p = urlparse(self.path).path
                if p.endswith("/auth/login"):
                    st["logins"] += 1
                    if st["on_login"]:
                        r = st["on_login"](st["logins"])
                        if r is not None:
                            return self._send(r, ctype="text/plain")
                    return self._send(b"Ok.", ctype="text/plain")
                st["calls"].append(("POST", p, q)); self._send(b"", ctype="text/plain")

            def do_GET(self):
                u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path.endswith("/torrents/info"):
                    st["info_calls"] += 1
                    if st["on_info"]:
                        st["on_info"](st["info_calls"])
                    if st["info_delay"]:
                        time.sleep(st["info_delay"])
                    return self._send(st["torrents"])
                if u.path.endswith("/torrents/files"):
                    return self._send(st["files"].get(q.get("hash"), []))
                if u.path.startswith("/api/v3/movie/"):
                    return self._send(st["movie"])
                if u.path.startswith("/api/v3/episodefile"):
                    return self._send(st["series_files"])
                self._send({}, 404)

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0); st["calls"].append(("PUT", urlparse(self.path).path, json.loads(self.rfile.read(n) or b"{}"))); self._send({})

            def do_DELETE(self):
                u = urlparse(self.path); st["calls"].append(("DELETE", u.path, {k: v[0] for k, v in parse_qs(u.query).items()}))
                if st["drop_delete"]:
                    self.connection.close(); return
                self._send({})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def torrent(h, content_path, seeding_days, ratio=0.1, state="stalledUP", save="/movies"):
    return {"hash": h, "name": h, "content_path": content_path, "save_path": save, "state": state, "ratio": ratio, "seeding_time": int(seeding_days * 86400),
            "completion_on": int(NOW - seeding_days * 86400), "added_on": int(NOW - seeding_days * 86400), "category": "movies", "size": 10}


P = "/media/Movies/A (2020)/a.mkv"


class RV(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.mnt = os.path.join(self.d, "mnt")
        self.jobs = os.path.join(self.d, "jobs")
        os.makedirs(os.path.join(self.jobs, "pending"))
        self.fake = Fake(); self.st = self.fake.state
        self.st["torrents"] = [torrent("music1", "/music/x", 1, save="/music")]
        self.addCleanup(self.fake.stop)
        self.addCleanup(shutil.rmtree, self.d, True)
        self.log = os.path.join(self.d, "job.log")

    # helpers (copied from test_e2e_runner)
    def lib(self, rel, data="data", disk="disk1"):
        p = os.path.join(self.mnt, disk, "Media", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
        return p

    def movie(self, *cp, mid=7):
        self.st["movie"] = {"id": mid, "title": "T", "monitored": True, "movieFile": {"path": cp[0]} if cp else None}

    def env(self, **kw):
        e = dict(os.environ, MOTHERSHIP_MNT=self.mnt, JOBS_DIR=self.jobs, QBIT_URL=self.fake.url, QBIT_USER="u", QBIT_PASS="p",
                 RADARR_URL=self.fake.url, RADARR_KEY="k", SONARR_URL=self.fake.url, SONARR_KEY="k", PLEX_TOKEN="", PLEX_URL="", PYTHONPATH=HERE,
                 PYTHONDONTWRITEBYTECODE="1", JOB_LOG=self.log)
        e.update(kw)
        return e

    def run_raw(self, **kw):
        return subprocess.run([sys.executable, os.path.join(HERE, "job_runner.py")], env=self.env(**kw), capture_output=True, text=True, timeout=180, cwd=self.d)

    def run_ok(self, **kw):
        r = self.run_raw(**kw)
        if r.returncode != 0:
            self.fail(f"runner crashed (rc={r.returncode}): {r.stderr[-600:]}")
        return r

    def submit(self, jid, paths=(P,), apply=False, digest="", **kw):
        job = dict(id=jid, type="delete_files", apply=apply, plan_digest=digest, created=time.time(), app="radarr", arr_id=7, title="T", paths=list(paths), remove_record=True)
        job.update(kw)
        with open(os.path.join(self.jobs, "pending", f"{jid}.json"), "w") as f:
            json.dump(job, f)

    def done(self, jid):
        with open(os.path.join(self.jobs, "done", f"{jid}.json")) as f:
            return json.load(f)

    def result(self, jid):
        return self.done(jid)["result"]

    def quarantined(self):
        return [os.path.join(r, f) for dk in ("disk1", "disk2") for r, _d, fs in os.walk(os.path.join(self.mnt, dk, "Media", ".quarantine")) for f in fs]

    def calls(self, method, frag=""):
        return [c for c in self.st["calls"] if c[0] == method and frag in c[1]]

    def plan(self, jid="aaaaaaaaaaaaaaa1", paths=(P,), **kw):
        self.submit(jid, paths, **kw); self.run_ok(); return self.result(jid)

    # ---------------------------------------------------------------- findings
    def test_01_symlinked_library_dir_pointing_outside_is_not_followed(self):
        """A directory symlink inside Movies/ that points at appdata: the file behind it must never be moved."""
        victim = self.lib("../appdata/plex/secret.db", "precious")           # disk1/appdata/plex/secret.db  (outside Media/)
        victim = os.path.abspath(victim)
        os.makedirs(os.path.join(self.mnt, "disk1", "Media", "Movies"), exist_ok=True)
        os.symlink(os.path.dirname(victim), os.path.join(self.mnt, "disk1", "Media", "Movies", "Linked"))
        cp = "/media/Movies/Linked/secret.db"; self.movie(cp)
        plan = self.plan(paths=[cp])
        self.submit("aaaaaaaaaaaaaaa2", [cp], apply=True, digest=plan.get("digest", "")); self.run_ok()
        self.assertTrue(os.path.exists(victim), f"file outside the library was moved into quarantine: {self.quarantined()}; plan={plan['lines']}")

    def test_02_apply_without_ever_planning_via_digest_leaked_by_the_refusal(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        self.submit("bbbbbbbbbbbbbbb1", apply=True, digest="0" * 16); self.run_ok()
        leaked = self.result("bbbbbbbbbbbbbbb1").get("digest")
        self.submit("bbbbbbbbbbbbbbb2", apply=True, digest=leaked); self.run_ok()
        self.assertTrue(os.path.exists(f), "an apply job with NO preceding plan job executed using the digest the runner handed back in its refusal")

    def test_03_failed_qbit_login_on_later_job_reuses_stale_index(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        digest = self.plan("cccccccccccccc01")["digest"]
        self.st["logins"] = 0

        def on_login(n):
            if n >= 2:                       # qBittorrent starts refusing logins (IP ban / restart) and a young seed now exists
                self.st["torrents"] = [torrent("young", "/movies/A (2020)/a.mkv", 1)]; self.st["files"]["young"] = [{"name": "A (2020)/a.mkv", "size": 1}]
                return b"Fails."
        self.st["on_login"] = on_login
        self.submit("cccccccccccccc02"); self.submit("cccccccccccccc03", apply=True, digest=digest)    # 02 logs in OK, 03 cannot
        self.run_ok()
        self.assertTrue(os.path.exists(f), f"apply ran on a stale qBittorrent view after login failed: {self.result('cccccccccccccc03')['lines']}")

    def test_04_torrents_without_content_path_make_everything_look_unseeded(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        t = torrent("seedy", "", 1); del t["content_path"]            # old/partial qBittorrent answer: no content_path at all
        t["name"] = "a.mkv"; t["save_path"] = "/movies/A (2020)"
        self.st["torrents"] = [t]
        plan = self.plan()
        self.assertNotIn("UNLINK", plan["lines"][0], plan["lines"])

    def test_05_bare_root_torrent_with_empty_file_list_looks_unseeded(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        self.st["torrents"] = [torrent("root", "/movies", 1)]; self.st["files"]["root"] = []     # partial answer
        plan = self.plan()
        self.assertNotIn("UNLINK", plan["lines"][0], plan["lines"])

    def test_06_malformed_job_shapes_must_not_crash_the_runner(self):
        self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        bodies = {"0001bad_created": json.dumps(dict(type="delete_files", apply=False, created="yesterday", app="radarr", arr_id=7, paths=[P])),
                  "0002bad_list": "[]", "0003bad_null": "null"}
        for jid, body in bodies.items():
            with open(os.path.join(self.jobs, "pending", jid + ".json"), "w") as fh:
                fh.write(body)
        self.submit("0009good")
        r = self.run_raw()
        missing = [j for j in list(bodies) + ["0009good"] if not os.path.exists(os.path.join(self.jobs, "done", j + ".json"))]
        self.assertEqual((r.returncode, missing), (0, []), r.stderr[-400:])

    def test_07_deeply_nested_json_is_a_permanent_poison_pill(self):
        with open(os.path.join(self.jobs, "pending", "0000poison.json"), "w") as fh:
            fh.write("[" * 200000 + "]" * 200000)
        self.lib("Movies/A (2020)/a.mkv"); self.movie(P); self.submit("0009good")
        r1 = self.run_raw(); r2 = self.run_raw()
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "done", "0009good.json")), f"rc1={r1.returncode} rc2={r2.returncode}; poison still pending={os.path.exists(os.path.join(self.jobs, 'pending', '0000poison.json'))}")

    def test_08_a_directory_named_x_json_is_a_permanent_poison_pill(self):
        os.makedirs(os.path.join(self.jobs, "pending", "0000dir.json"))
        self.lib("Movies/A (2020)/a.mkv"); self.movie(P); self.submit("0009good")
        r1 = self.run_raw(); r2 = self.run_raw()
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "done", "0009good.json")), f"rc1={r1.returncode} rc2={r2.returncode}: {r2.stderr[-300:]}")

    def test_09_interrupted_sweep_overwrites_a_real_result(self):
        os.makedirs(os.path.join(self.jobs, "running")); os.makedirs(os.path.join(self.jobs, "done"))
        with open(os.path.join(self.jobs, "running", "dddddddddddddd01.json"), "w") as fh:
            json.dump({"id": "x"}, fh)
        with open(os.path.join(self.jobs, "done", "dddddddddddddd01.json"), "w") as fh:        # runner died between write_done() and os.remove(running)
            json.dump({"id": "x", "result": {"ok": True, "lines": ["quarantined: a.mkv"]}}, fh)
        self.run_ok()
        self.assertTrue(self.result("dddddddddddddd01")["ok"], "a successful result was replaced by 'interrupted'")

    def test_10_wrapper_never_reaps_stale_running_jobs_when_nothing_is_pending(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        bindir = os.path.join(d, "bin"); os.makedirs(bindir); marker = os.path.join(d, "docker_called")
        with open(os.path.join(bindir, "docker"), "w") as fh:
            fh.write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(os.path.join(bindir, "docker"), 0o755)
        os.makedirs(os.path.join(self.jobs, "running"))
        open(os.path.join(self.jobs, "running", "eeeeeeeeeeeeee01.json"), "w").write("{}")      # an apply that was claimed when the runner was killed
        sh = open(os.path.join(HERE, "dashboard_jobs.sh")).read().replace("/mnt/cache/appdata/plex-dashboard/jobs", self.jobs).replace("/mnt/user/system/scripts/mothership", d).replace("/tmp/dashboard_jobs.lock", os.path.join(d, "lock"))
        s = os.path.join(d, "dj.sh"); open(s, "w").write(sh)
        subprocess.run(["bash", s], env=dict(os.environ, PATH=bindir + ":" + os.environ["PATH"]), timeout=30)
        self.assertTrue(os.path.exists(marker), "no pending job -> wrapper exits before the runner can report the interrupted job; the dashboard waits forever")

    def test_11_second_runner_while_first_is_mid_job_marks_it_interrupted_and_crashes_it(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        self.st["info_delay"] = 4
        self.submit("ffffffffffffff01")
        p1 = subprocess.Popen([sys.executable, os.path.join(HERE, "job_runner.py")], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.d)
        for _ in range(100):
            if os.path.exists(os.path.join(self.jobs, "running", "ffffffffffffff01.json")):
                break
            time.sleep(0.1)
        self.run_ok()                                   # operator (or a lock-less second invocation) starts another runner
        out, err = p1.communicate(timeout=60)
        self.assertEqual(p1.returncode, 0, f"first runner crashed after the second swept its running/ file: {err[-300:]}")
        self.assertNotEqual(self.done("ffffffffffffff01").get("type"), "interrupted")

    def test_12_arr_failure_after_quarantine_hides_what_was_done(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan()
        self.st["drop_delete"] = True
        self.submit("aaaaaaaaaaaaaaa2", apply=True, digest=plan["digest"]); self.run_ok()
        res = self.result("aaaaaaaaaaaaaaa2")
        self.assertFalse(os.path.exists(f))                      # it WAS quarantined ...
        self.assertTrue(any("quarantin" in l.lower() for l in res["lines"]), f"... but the result only says: {res['lines']}")

    def test_13_duplicate_paths_plan_promises_record_removal_apply_keeps_it(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan(paths=[P, P])
        self.submit("aaaaaaaaaaaaaaa2", [P, P], apply=True, digest=plan["digest"]); self.run_ok()
        res = self.result("aaaaaaaaaaaaaaa2")
        self.assertEqual(bool(plan.get("removes_record")), bool(self.calls("DELETE", "/movie/7")), f"plan.removes_record={plan.get('removes_record')} but apply lines={res['lines']}")

    def test_14_future_dated_created_defeats_the_30_minute_expiry(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan(created=time.time() + 10 ** 9)
        self.submit("aaaaaaaaaaaaaaa2", apply=True, digest=plan["digest"], created=time.time() + 10 ** 9); self.run_ok()
        self.assertTrue(os.path.exists(f), "a job whose 'created' is in the year 2050 was executed; expiry is attacker-controlled")

    def test_15_title_with_newline_forges_log_lines(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan()
        self.submit("aaaaaaaaaaaaaaa2", apply=True, digest=plan["digest"], title="x\n[2026-01-01 00:00:00] QUARANTINE /mnt/disk9/forged -> nowhere"); self.run_ok()
        lines = open(self.log).read().splitlines()
        self.assertFalse([l for l in lines if l.startswith("[2026-01-01 00:00:00] QUARANTINE /mnt/disk9/forged")], "attacker-controlled title wrote a forged log record")

    def test_16_plex_token_not_written_to_runner_output(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan()
        self.submit("aaaaaaaaaaaaaaa2", apply=True, digest=plan["digest"])
        r = self.run_ok(PLEX_TOKEN="SECRETPLEXTOKEN123", PLEX_URL="http://127.0.0.1:9")
        blob = r.stdout + r.stderr + open(self.log).read() + open(os.path.join(self.jobs, "done", "aaaaaaaaaaaaaaa2.json")).read()
        self.assertNotIn("SECRETPLEXTOKEN123", blob)

    def test_17_half_written_job_is_destroyed_not_retried(self):
        self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        pth = os.path.join(self.jobs, "pending", "aaaaaaaaaaaaaaa1.json")
        full = json.dumps(dict(id="a", type="delete_files", apply=False, created=time.time(), app="radarr", arr_id=7, title="T", paths=[P], remove_record=True))
        open(pth, "w").write(full[:40])                      # dashboard is still writing it (mtime = now)
        self.run_ok()
        self.assertTrue(os.path.exists(pth), "a file modified a moment ago was judged unreadable and deleted; the dashboard's finished write is lost")

    def test_18_torrent_removed_between_jobs_is_not_remembered_by_stale_index(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        self.st["torrents"] = [torrent("old", "/movies/A (2020)/a.mkv", 200, ratio=2)]; self.st["files"]["old"] = [{"name": "A (2020)/a.mkv", "size": 1}]

        def on_info(n):
            if n >= 2:
                self.st["torrents"] = [torrent("music1", "/music/x", 1, save="/music")]       # the torrent was removed in qBittorrent between jobs
        self.st["on_info"] = on_info
        self.submit("1job"); self.submit("2job"); self.run_ok()
        l2 = self.result("2job")["lines"][0]
        self.assertIn("UNLINK", l2, f"job 2 after the torrent is gone still sees it through dr.QBIT_INDEX (never cleared): {l2}")

    def test_19_apply_flag_as_string_false_is_treated_as_apply(self):
        f = self.lib("Movies/A (2020)/a.mkv"); self.movie(P)
        plan = self.plan()
        self.submit("aaaaaaaaaaaaaaa2", apply="false", digest=plan["digest"]); self.run_ok()
        self.assertTrue(os.path.exists(f), "apply='false' (a string) executed the removal")

    def test_20_notify_malformed_json_is_swallowed_and_deleted(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        out = os.path.join(d, "outbox"); os.makedirs(out); rec = os.path.join(d, "rec")
        stub = os.path.join(d, "notify"); open(stub, "w").write(f"#!/bin/sh\necho \"$@\" >> {rec}\n"); os.chmod(stub, 0o755)
        sh = open(os.path.join(HERE, "dashboard_notify.sh")).read().replace("/mnt/cache/appdata/plex-dashboard/outbox", out).replace("/usr/local/emhttp/webGui/scripts/notify", stub)
        s = os.path.join(d, "n.sh"); open(s, "w").write(sh)
        bad = os.path.join(out, "1.json"); open(bad, "w").write('{"subject": "Disk fail", "mess')    # truncated write
        subprocess.run(["bash", s], timeout=30, capture_output=True)
        sent = open(rec).read() if os.path.exists(rec) else ""
        self.assertTrue(os.path.exists(bad) or "Disk fail" in sent, f"truncated notification was deleted after sending a blank notification: {sent!r}")


if __name__ == "__main__":
    unittest.main()
