"""Hostile-review round C, shell scripts. Each test encodes the CORRECT behaviour and FAILS on the current code."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jq_shim.py")


def install_jq(bindir):
    """real jq is absent in the review sandbox: use an emulation of the exact filters (jq_shim.py) unless a real jq exists"""
    os.makedirs(bindir, exist_ok=True)
    if shutil.which("jq"):
        return
    p = os.path.join(bindir, "jq")
    open(p, "w").write("#!/bin/bash\nexec python3 %s \"$@\"\n" % SHIM); os.chmod(p, 0o755)


class Srv:
    """Tiny HTTP server; handler(method, path, query, body) -> (status, body str)."""

    def __init__(self, handler):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                u = urlparse(self.path)
                outer.calls.append((method, u.path, parse_qs(u.query), body))
                st, out = handler(method, u.path, parse_qs(u.query), body)
                self.send_response(st); self.send_header("Content-Length", str(len(out.encode()))); self.end_headers(); self.wfile.write(out.encode())

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")
        self.s = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.s.server_address[1]
        threading.Thread(target=self.s.serve_forever, daemon=True).start()

    def stop(self):
        self.s.shutdown()


class TagTracker(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        src = open(os.path.join(HERE, "tag-tracker-issues.sh")).read()
        src = src.replace('/logs/tag-tracker-issues.log', self.d + '/log.txt').replace('/tmp/tag-tracker-issues"', self.d + '/work"') \
                 .replace("/tmp/tag-tracker-issues.lock", self.d + "/lock")
        self.sp = os.path.join(self.d, "t.sh"); open(self.sp, "w").write(src)
        install_jq(self.d + "/bin")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_sh(self, srv, *args):
        return subprocess.run(["bash", self.sp, *args], env=dict(os.environ, PATH=self.d + "/bin:" + os.environ["PATH"], QBIT_URL=f"http://127.0.0.1:{srv.port}", QBIT_PASS="x"), capture_output=True, text=True, timeout=60)

    def test_C20_tracker_query_failures_wipe_existing_tags(self):
        def h(m, p, q, b):
            if p.endswith("/auth/login"):
                return 200, "Ok."
            if p.endswith("/torrents/info"):
                return 200, json.dumps([{"hash": "AAA"}]) if "tag=" not in b else json.dumps([{"hash": "AAA"}])
            if p.endswith("/torrents/trackers"):
                return 500, "Internal Server Error"      # transient failure (or 403 after the session expired mid-run)
            return 200, ""
        s = Srv(h)
        try:
            r = self.run_sh(s, "--apply")
        finally:
            s.stop()
        removed = [c for c in s.calls if c[1].endswith("/removeTags")]
        self.assertEqual(removed, [], f"every tag removed because the per-torrent tracker query failed; out={r.stdout[-300:]}")

    def test_C21_login_fails_text_with_http_200_is_treated_as_success(self):
        def h(m, p, q, b):
            if p.endswith("/auth/login"):
                return 200, "Fails."
            return 403, "Forbidden"
        s = Srv(h)
        try:
            r = self.run_sh(s)
        finally:
            s.stop()
        self.assertNotEqual(r.returncode, 0, f"login answered 'Fails.' and torrents/info answered 403, yet the run 'succeeds': {r.stdout[-300:]}")


class LibraryHealth(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        d = self.d
        for sub in ("mnt/user/system/scripts/mothership", "mnt/docker/radarr", "mnt/docker/sonarr", "tmp", "bin"):
            os.makedirs(os.path.join(d, sub))
        src = open(os.path.join(HERE, "library_health.sh")).read()
        import re
        src = re.sub(r"/usr/local/emhttp/webGui/scripts/notify|/mnt/|/tmp/", lambda m: d + "/notify" if "notify" in m.group(0) else d + m.group(0), src)
        self.sp = os.path.join(d, "lh.sh"); open(self.sp, "w").write(src)
        open(os.path.join(d, "bin", "docker"), "w").write("#!/bin/bash\necho 127.0.0.1\n"); os.chmod(os.path.join(d, "bin", "docker"), 0o755)
        open(os.path.join(d, "notify"), "w").write("#!/bin/bash\necho \"$@\" >> %s/notified\n" % d); os.chmod(os.path.join(d, "notify"), 0o755)
        self.srvs = []
        install_jq(self.d + "/bin")

    def tearDown(self):
        for s in self.srvs:
            s.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def start(self, radarr, sonarr, qbit=None):
        r, s, q = Srv(radarr), Srv(sonarr), Srv(qbit or (lambda m, p, qq, b: (200, "Ok.") if p.endswith("login") else (200, json.dumps([{"state": "uploading"}]))))
        self.srvs += [r, s, q]
        for app, srv in (("radarr", r), ("sonarr", s)):
            open(os.path.join(self.d, "mnt/docker", app, "config.xml"), "w").write(f"<Config><ApiKey>k</ApiKey><Port>{srv.port}</Port></Config>")
        open(os.path.join(self.d, "mnt/user/system/scripts/mothership/.env"), "w").write(f"QBIT_URL=http://127.0.0.1:{q.port}\nQBIT_USER=a\nQBIT_PASS=b\n")

    def go(self):
        return subprocess.run(["bash", self.sp, "--no-notify"], env=dict(os.environ, PATH=self.d + "/bin:" + os.environ["PATH"]), capture_output=True, text=True, timeout=120)

    def test_C30_file_on_one_of_two_disks_is_reported_missing(self):
        movie = [{"hasFile": True, "path": "/media/Movies/A", "movieFile": {"path": "/media/Movies/A/a.mkv", "relativePath": "a.mkv"}}]

        def radarr(m, p, q, b):
            return (200, json.dumps({"version": "5"})) if p.endswith("system/status") else (200, json.dumps(movie)) if p.endswith("/movie") else (200, json.dumps({"records": []}))

        def sonarr(m, p, q, b):
            return (200, json.dumps({"version": "4"})) if p.endswith("system/status") else (200, "[]") if p.endswith("/series") else (200, json.dumps({"records": []}))
        self.start(radarr, sonarr)
        os.makedirs(os.path.join(self.d, "mnt/disk1/Media/Movies/A")); open(os.path.join(self.d, "mnt/disk1/Media/Movies/A/a.mkv"), "w").write("x")
        os.makedirs(os.path.join(self.d, "mnt/disk2/Media"))
        r = self.go()
        self.assertIn("Radarr tracked files missing on disk: 0", r.stdout, r.stdout[-600:])

    def test_C31_api_error_bodies_after_a_good_status_read_as_all_clear(self):
        def radarr(m, p, q, b):
            return (200, json.dumps({"version": "5"})) if p.endswith("system/status") else (503, "<html>Service Unavailable</html>")

        def sonarr(m, p, q, b):
            return (200, json.dumps({"version": "4"})) if p.endswith("system/status") else (200, json.dumps({"message": "Database is locked"}))
        self.start(radarr, sonarr)
        r = self.go()
        self.assertNotEqual(r.returncode, 0, "movie/series/queue reads all failed (503 / error object) but the script reports: " + r.stdout[-400:])

    def test_C32_only_first_200_queue_items_are_examined(self):
        def radarr(m, p, q, b):
            if p.endswith("system/status"):
                return 200, json.dumps({"version": "5"})
            if p.endswith("/queue"):
                if q.get("page", ["1"])[0] == "1":
                    return 200, json.dumps({"page": 1, "pageSize": 200, "totalRecords": 400, "records": [{"trackedDownloadState": "downloading"}] * 200})
                return 200, json.dumps({"page": 2, "records": [{"trackedDownloadState": "importBlocked"}] * 200})
            return 200, "[]"

        def sonarr(m, p, q, b):
            return (200, json.dumps({"version": "4"})) if p.endswith("system/status") else (200, "[]") if p.endswith("/series") else (200, json.dumps({"records": []}))
        self.start(radarr, sonarr)
        r = self.go()
        self.assertIn("radarr queue items blocked/warning: 200", r.stdout, "200 blocked items sit on page 2 of the queue and are never read: " + r.stdout[-500:])


class FixPerms(unittest.TestCase):
    def test_C40_dry_run_default_changes_nothing(self):
        # sanity: the script has no unsafe default (kept as a 'could not break' probe)
        src = open(os.path.join(HERE, "fix-library-perms.sh")).read()
        self.assertIn('[ "$1" = "--apply" ]', src)


if __name__ == "__main__":
    unittest.main()
