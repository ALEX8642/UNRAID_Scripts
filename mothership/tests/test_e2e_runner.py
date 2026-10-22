"""End-to-end: the REAL job_runner.py + dupe_review.py + safe_ops.py in a subprocess, against a scratch library tree and a fake
qBittorrent/Radarr HTTP server. Nothing is mocked inside the code under test - these are the scenarios the reviewers attacked."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import requests  # noqa: F401
    import rich  # noqa: F401
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOW = time.time()


class Fake:
    """qBittorrent + Radarr in one tiny server. `state` is shared with the test."""

    def __init__(self):
        self.state = {"torrents": [], "files": {}, "movie": {}, "calls": []}
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
                    return self._send(b"Ok.", ctype="text/plain")
                st["calls"].append(("POST", p, q)); self._send(b"", ctype="text/plain")

            def do_GET(self):
                u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path.endswith("/torrents/info"):
                    return self._send(st["torrents"])
                if u.path.endswith("/torrents/files"):
                    return self._send(st["files"].get(q.get("hash"), []))
                if u.path.startswith("/api/v3/movie/"):
                    return self._send(st["movie"])
                self._send({}, 404)

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0); st["calls"].append(("PUT", urlparse(self.path).path, json.loads(self.rfile.read(n) or b"{}"))); self._send({})

            def do_DELETE(self):
                u = urlparse(self.path); st["calls"].append(("DELETE", u.path, {k: v[0] for k, v in parse_qs(u.query).items()})); self._send({})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def torrent(h, content_path, seeding_days, ratio=0.1, state="stalledUP", completed_days=None, save="/movies"):
    cd = seeding_days if completed_days is None else completed_days
    return {"hash": h, "name": h, "content_path": content_path, "save_path": save, "state": state, "ratio": ratio, "seeding_time": int(seeding_days * 86400),
            "completion_on": int(NOW - cd * 86400), "added_on": int(NOW - cd * 86400), "category": "movies", "size": 10}


@unittest.skipUnless(HAVE_DEPS, "needs requests and rich (pip install requests rich)")
class E2E(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.mnt = os.path.join(self.d, "mnt")
        self.jobs = os.path.join(self.d, "jobs")
        os.makedirs(os.path.join(self.jobs, "pending"))
        self.fake = Fake()
        self.st = self.fake.state
        self.st["torrents"] = [torrent("music1", "/music/x", 1, save="/music")]      # a real qBittorrent is never empty; an empty list is itself a refusal
        self.addCleanup(self.fake.stop)

    # --- helpers
    def lib(self, rel, data="data", disk="disk1"):
        p = os.path.join(self.mnt, disk, "Media", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
        return p

    def movie(self, *container_paths, mid=7):
        mf = {"path": container_paths[0]} if container_paths else None
        self.st["movie"] = {"id": mid, "title": "T", "monitored": True, "movieFile": mf}

    def run_runner(self, qbit_url=None):
        env = dict(os.environ, MOTHERSHIP_MNT=self.mnt, JOBS_DIR=self.jobs, QBIT_URL=qbit_url or self.fake.url, QBIT_USER="u", QBIT_PASS="p",
                   RADARR_URL=self.fake.url, RADARR_KEY="k", SONARR_URL=self.fake.url, SONARR_KEY="k", PLEX_TOKEN="", PLEX_URL="", PYTHONPATH=HERE,
                   PYTHONDONTWRITEBYTECODE="1", JOB_LOG=os.path.join(self.d, "job.log"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "job_runner.py")], env=env, capture_output=True, text=True, timeout=120, cwd=self.d)
        if r.returncode != 0:
            self.fail(f"runner crashed: {r.stderr[-800:]}")

    def submit(self, jid, paths, apply=False, digest="", **kw):
        job = dict(id=jid, type="delete_files", apply=apply, plan_digest=digest, created=time.time(), app="radarr", arr_id=7, title="T", paths=list(paths), remove_record=True)
        job.update(kw)
        with open(os.path.join(self.jobs, "pending", f"{jid}.json"), "w") as f:
            json.dump(job, f)

    def result(self, jid):
        with open(os.path.join(self.jobs, "done", f"{jid}.json")) as f:
            return json.load(f)["result"]

    def plan_then_apply(self, paths, **kw):
        self.submit("aaaaaaaaaaaaaaa1", paths, **kw); self.run_runner(); plan = self.result("aaaaaaaaaaaaaaa1")
        self.submit("aaaaaaaaaaaaaaa2", paths, apply=True, digest=plan.get("digest", ""), **kw); self.run_runner()
        return plan, self.result("aaaaaaaaaaaaaaa2")

    def quarantined(self):
        return [os.path.join(r, f) for dk in ("disk1", "disk2") for r, _d, fs in os.walk(os.path.join(self.mnt, dk, "Media", ".quarantine")) for f in fs]

    def calls(self, method, frag=""):
        return [c for c in self.st["calls"] if c[0] == method and frag in c[1]]

    # --- scenarios
    def test_loose_unseeded_file_is_quarantined_and_record_removed(self):
        f = self.lib("Movies/Loose (2020)/loose.mkv"); P = "/media/Movies/Loose (2020)/loose.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("zz", "/music/x", 1, save="/music")]
        plan, res = self.plan_then_apply([P])
        self.assertIn("UNLINK", plan["lines"][0]); self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(f)); self.assertEqual(len(self.quarantined()), 1); self.assertEqual(open(self.quarantined()[0]).read(), "data")
        self.assertEqual(len(self.calls("DELETE", "/movie/7")), 1); self.assertEqual(self.calls("DELETE", "/movie/7")[0][2]["deleteFiles"], "false")
        self.assertEqual(self.calls("POST", "/torrents/"), [])

    def test_young_seed_keeps_seeding_is_tagged_and_the_record_survives(self):
        f = self.lib("Movies/Seeded (2020)/s.mkv"); P = "/media/Movies/Seeded (2020)/s.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("young", "/movies/Seeded (2020)/s.mkv", 1)]; self.st["files"]["young"] = [{"name": "Seeded (2020)/s.mkv", "size": 10**9}]
        plan, res = self.plan_then_apply([P])
        self.assertIn("TAG_ONLY", plan["lines"][0])
        self.assertTrue(os.path.exists(f)); self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.calls("DELETE"), []); self.assertEqual(self.calls("POST", "torrents/delete"), [])
        self.assertEqual(self.calls("POST", "addTags")[0][2]["tags"], "delete-later")
        self.assertFalse(self.calls("PUT", "/movie/7")[0][2]["monitored"])           # unmonitored so Radarr stops touching it

    def test_cross_seeded_young_torrent_protects_the_file_even_if_the_other_is_ancient(self):
        f = self.lib("Movies/X (2020)/x.mkv"); P = "/media/Movies/X (2020)/x.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("young", "/movies/X (2020)/x.mkv", 2), torrent("old", "/movies/X (2020)/x.mkv", 300)]
        for h in ("young", "old"):
            self.st["files"][h] = [{"name": "X (2020)/x.mkv", "size": 10**9}]
        for order in (False, True):                                 # the answer must not depend on listing order
            self.st["torrents"].sort(key=lambda t: t["hash"], reverse=order)
            plan, res = self.plan_then_apply([P])
            self.assertNotIn("QBIT_DELETE", plan["lines"][0]); self.assertTrue(os.path.exists(f), order)
            self.assertEqual(self.calls("POST", "torrents/delete"), [])
            for jid in ("aaaaaaaaaaaaaaa1", "aaaaaaaaaaaaaaa2"):
                os.remove(os.path.join(self.jobs, "done", f"{jid}.json"))

    def test_seasoned_library_owned_seed_is_quarantined_and_qbit_never_deletes_files(self):
        f = self.lib("Movies/Old (2010)/o.mkv"); P = "/media/Movies/Old (2010)/o.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("old", "/movies/Old (2010)/o.mkv", 200, ratio=2.0)]; self.st["files"]["old"] = [{"name": "Old (2010)/o.mkv", "size": 10**9}]
        plan, res = self.plan_then_apply([P])
        self.assertIn("QBIT_DELETE", plan["lines"][0]); self.assertFalse(os.path.exists(f)); self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(self.calls("POST", "torrents/delete")[0][2]["deleteFiles"], "false")

    def test_hardlinked_library_name_of_an_arr_seed_is_quarantined_before_the_seed_is_removed(self):
        seed = self.lib("arr/movies/X (2020)/x.mkv", "bytes"); lib = os.path.join(self.mnt, "disk1", "Media", "Movies", "X (2020)", "x.mkv")
        os.makedirs(os.path.dirname(lib)); os.link(seed, lib); P = "/media/Movies/X (2020)/x.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("seed", "/arr/movies/X (2020)/x.mkv", 300, ratio=3, save="/arr/movies")]; self.st["files"]["seed"] = [{"name": "X (2020)/x.mkv", "size": 10**9}]
        plan, res = self.plan_then_apply([P])
        self.assertIn("LIBRARY_NAME_THEN_DELETE", plan["lines"][0]); self.assertFalse(os.path.exists(lib))
        q = self.quarantined(); self.assertEqual(len(q), 1); self.assertEqual(open(q[0]).read(), "bytes")      # the bytes survive in quarantine
        self.assertEqual(self.calls("POST", "torrents/delete")[0][2]["deleteFiles"], "true")

    def test_names_with_glob_characters_do_not_hit_a_lookalike(self):
        a = self.lib("Movies/Foo [2020]/a.mkv", "right"); b = self.lib("Movies/Foo 2/a.mkv", "other"); P = "/media/Movies/Foo [2020]/a.mkv"; self.movie(P)
        self.plan_then_apply([P])
        self.assertFalse(os.path.exists(a)); self.assertTrue(os.path.exists(b)); self.assertEqual(open(b).read(), "other")

    def test_qbittorrent_unreachable_means_nothing_is_touched(self):
        f = self.lib("Movies/A (2020)/a.mkv"); P = "/media/Movies/A (2020)/a.mkv"; self.movie(P)
        self.submit("bbbbbbbbbbbbbbb1", [P]); self.run_runner(qbit_url="http://127.0.0.1:9")
        res = self.result("bbbbbbbbbbbbbbb1")
        self.assertFalse(res["ok"]); self.assertTrue(os.path.exists(f)); self.assertEqual(self.quarantined(), [])

    def test_an_unindexable_torrent_location_blocks_deletion(self):
        f = self.lib("Movies/A (2020)/a.mkv"); P = "/media/Movies/A (2020)/a.mkv"; self.movie(P)
        self.st["torrents"] = [torrent("w", "/weird/mount/thing", 1, save="/weird/mount")]
        plan, res = self.plan_then_apply([P])
        self.assertFalse(res["ok"]); self.assertTrue(os.path.exists(f))

    def test_tampered_digest_or_changed_world_stops_the_apply(self):
        f = self.lib("Movies/A (2020)/a.mkv"); P = "/media/Movies/A (2020)/a.mkv"; self.movie(P)
        self.submit("ccccccccccccccc1", [P]); self.run_runner()
        self.submit("ccccccccccccccc2", [P], apply=True, digest="0" * 16); self.run_runner()
        self.assertFalse(self.result("ccccccccccccccc2")["ok"]); self.assertTrue(os.path.exists(f))
        # world changes between plan and apply: a seed appears
        self.submit("ccccccccccccccc3", [P]); self.run_runner(); digest = self.result("ccccccccccccccc3")["digest"]
        self.st["torrents"] = [torrent("new", "/movies/A (2020)/a.mkv", 1)]; self.st["files"]["new"] = [{"name": "A (2020)/a.mkv", "size": 10**9}]
        self.submit("ccccccccccccccc4", [P], apply=True, digest=digest); self.run_runner()
        self.assertFalse(self.result("ccccccccccccccc4")["ok"]); self.assertTrue(os.path.exists(f))

    def test_files_the_record_does_not_own_are_never_touched(self):
        mine = self.lib("Movies/A (2020)/a.mkv"); other = self.lib("Movies/B (2021)/b.mkv"); self.movie("/media/Movies/A (2020)/a.mkv")
        self.submit("ddddddddddddddd1", ["/media/Movies/B (2021)/b.mkv"], apply=True, digest="x"); self.run_runner()
        self.assertFalse(self.result("ddddddddddddddd1")["ok"]); self.assertTrue(os.path.exists(other)); self.assertTrue(os.path.exists(mine))

    def test_an_unexplained_extra_hardlink_refuses(self):
        f = self.lib("Movies/A (2020)/a.mkv"); extra = os.path.join(self.mnt, "disk1", "Media", "somewhere", "a.mkv"); os.makedirs(os.path.dirname(extra)); os.link(f, extra)
        P = "/media/Movies/A (2020)/a.mkv"; self.movie(P)
        plan, res = self.plan_then_apply([P])
        self.assertIn("REFUSE", plan["lines"][0]); self.assertTrue(os.path.exists(f)); self.assertEqual(self.quarantined(), [])

    def test_a_file_on_two_disks_is_refused(self):
        a = self.lib("Movies/A (2020)/a.mkv", disk="disk1"); b = self.lib("Movies/A (2020)/a.mkv", disk="disk2"); P = "/media/Movies/A (2020)/a.mkv"; self.movie(P)
        plan, res = self.plan_then_apply([P])
        self.assertTrue(os.path.exists(a) and os.path.exists(b)); self.assertEqual(self.quarantined(), [])

    def test_symlink_and_directory_paths_are_refused(self):
        real = self.lib("Movies/A (2020)/real.mkv"); link = os.path.join(os.path.dirname(real), "link.mkv"); os.symlink(real, link)
        self.movie("/media/Movies/A (2020)/link.mkv")
        plan, res = self.plan_then_apply(["/media/Movies/A (2020)/link.mkv"])
        self.assertTrue(os.path.exists(real)); self.assertEqual(self.quarantined(), [])


if __name__ == "__main__":
    unittest.main()
