"""Hostile-review round C: each test encodes the CORRECT behaviour and FAILS on the current code."""
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(__file__))
for m in ("rich", "rich.console", "rich.table", "rich.progress", "rich.box"):
    sys.modules.setdefault(m, mock.MagicMock())
import seed_cleanup as sc  # noqa: E402
import test_seed_cleanup as tsc  # noqa: E402
import test_hnr_guard as thg  # noqa: E402
import orphan_scan as os_  # noqa: E402
import quarantine as q  # noqa: E402

NOW = time.time()


class SeedCleanupC(unittest.TestCase):
    def test_C1_zero_or_negative_seed_days_env_is_floored(self):
        """safe_ops floors HNR_MIN_SEED_DAYS at 5 ("a typo of 0 ... must never turn every seed into seasoned"); seed_cleanup does not."""
        for val in ("0", "-3"):
            code = ("import sys,time;sys.path.insert(0,%r);import seed_cleanup as sc;now=time.time();"
                    "t=dict(hash='a',name='n',state='stalledUP',completion_on=now-3600,ratio=0.0,seeding_time=0);"
                    "print(sc.seed_rule_met(t,now))" % HERE)
            out = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, HNR_MIN_SEED_DAYS=val), capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "False", f"HNR_MIN_SEED_DAYS={val}: torrent completed 1 h ago, 0 ratio, 0 s seeded counted as seed rule met ({out.stderr[-200:]})")

    def test_C2_all_data_disks_unmounted_still_trips_the_missing_disk_guard(self):
        with self.assertRaises(SystemExit):
            tsc.run([tsc.torrent("a", "Movie.A")], tsc.UP, state={**tsc.OLD, "_disks": 12}, disks=0)

    def test_C3_partial_pack_removed_although_most_of_its_files_still_exist(self):
        """Only the 3 largest files are inspected; a 10-episode pack whose 3 biggest episodes were upgraded away is removed while 7 files still seed."""
        names = [f"Show.S01E{i:02d}.mkv" for i in range(10)]
        files = [{"name": f"Show.S01/{n}", "size": (10 - i) * 10**9} for i, n in enumerate(names)]      # E00..E02 are the biggest
        t = tsc.torrent("a", "Show.S01", content_path="/tv/Show.S01", save_path="/tv")
        fs = tsc.FakeSession([t], {"a": files})
        gone = {f"/tv/Show.S01/{n}" for n in names[:3]}
        up = {n: NOW - 20 * 86400 for n in names[:3]}
        out = io.StringIO()
        with mock.patch.object(sc.requests, "Session", return_value=fs), mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p not in gone), \
                mock.patch.object(sc, "upgrade_deleted_names", return_value=up), mock.patch("os.path.isdir", return_value=True), \
                mock.patch.object(sc, "load_state", return_value=dict(tsc.OLD)), mock.patch.object(sc, "save_state"), mock.patch.object(sc, "mounted_disks", return_value=3), \
                mock.patch.dict(os.environ, {"SEED_CLEANUP_LOCK": os.path.join(tempfile.mkdtemp(), "lock")}), mock.patch.object(sys, "argv", ["x", "--apply"]), redirect_stdout(out):
            sc.main()
        self.assertEqual(fs.deleted, [], "7 of 10 files of the pack are still on the array but the torrent was removed")

    def test_C4_history_beyond_20_pages_is_silently_ignored(self):
        calls = {"n": 0}

        def get(url, params=None, **kw):
            calls["n"] += 1
            r = mock.MagicMock()
            page = params["page"]
            rec = {"data": {"reason": "Upgrade"}, "sourceTitle": f"x{page}.mkv", "date": "2026-10-01T00:00:00Z"}
            r.json.return_value = {"records": [rec], "totalRecords": 100000}
            return r
        out = io.StringIO()
        with mock.patch.object(sc.requests, "get", side_effect=get), redirect_stdout(out):
            try:
                names = sc.upgrade_deleted_names("radarr")
                warned = "truncat" in out.getvalue().lower() or "limit" in out.getvalue().lower()
            except Exception:
                warned = True      # raising is also acceptable
        self.assertTrue(warned, f"stopped after {calls['n']} pages although totalRecords=100000, with no warning or error")

    def test_C5_torrent_state_is_rechecked_just_before_delete(self):
        """The torrent list is read once, then 20 history pages (up to 120 s each) are read; nothing re-asks qBittorrent before removing."""
        t = tsc.torrent("a", "Movie.A")
        fs = tsc.FakeSession([t], {"a": [{"name": "Movie.A/Movie.A.mkv", "size": 5}]})
        info_calls = {"n": 0}
        orig_get = fs.get

        def get(url, params=None, **kw):
            if url.endswith("/torrents/info"):
                info_calls["n"] += 1
            return orig_get(url, params, **kw)
        fs.get = get
        with mock.patch.object(sc.requests, "Session", return_value=fs), mock.patch.object(sc, "exists_on_array", return_value=False), \
                mock.patch.object(sc, "upgrade_deleted_names", return_value=tsc.UP), mock.patch("os.path.isdir", return_value=True), \
                mock.patch.object(sc, "load_state", return_value=dict(tsc.OLD)), mock.patch.object(sc, "save_state"), mock.patch.object(sc, "mounted_disks", return_value=3), \
                mock.patch.dict(os.environ, {"SEED_CLEANUP_LOCK": os.path.join(tempfile.mkdtemp(), "lock")}), mock.patch.object(sys, "argv", ["x", "--apply"]), redirect_stdout(io.StringIO()):
            sc.main()
        self.assertEqual(fs.deleted, ["a"])
        self.assertGreaterEqual(info_calls["n"], 2, "removed from a stale snapshot without re-reading the torrent's current state/content")


class HnrC(unittest.TestCase):
    def test_C6_second_young_torrent_for_an_already_held_movie_is_not_recorded(self):
        # movie 1 is held (unmonitored) for torrent a; a is now satisfied, but ANOTHER young torrent b seeds the same file name
        e = thg.Env([thg.tor("a", "A", 6), thg.tor("b", "B", 1)], {"a": ["A/a.mkv"], "b": ["B/a.mkv"]}, [thg.movie(1, "a.mkv", monitored=False)], state=thg.hold("a"))
        e.run()
        self.assertEqual(e.puts, [], "movie re-monitored while young torrent b still seeds the same file")

    def test_C7_hold_and_release_for_same_item_in_one_run_ends_monitored(self):
        e = thg.Env([thg.tor("a", "A", 6), thg.tor("b", "B", 1)], {"a": ["A/a.mkv"], "b": ["B/a.mkv"]}, [thg.movie(1, "a.mkv", monitored=True)], state=thg.hold("a"))
        e.run()
        self.assertNotEqual(e.puts[-1:] , [("radarr", "movie/1", True)], f"final PUT re-monitors a movie whose young torrent b is seeding: {e.puts}")

    def test_C8_absent_counter_is_shared_by_different_hashes(self):
        # hashes [a, b]; run 1: a was absent (absent=1). run 2: a is back and satisfied, b is absent for the FIRST time.
        st = {"radarr": {"1": {"title": "M1", "hashes": ["a", "b"], "absent": 1}}, "sonarr": {}, "_n": 0}
        e = thg.Env([thg.tor("a", "A", 9), thg.tor("z", "Z", 9)], {"a": ["a.mkv"], "z": ["z.mkv"]}, [thg.movie(1, "a.mkv", monitored=False)], state=st)
        e.run()
        self.assertEqual(e.puts, [], "b has been absent only once, yet the hold was released")

    def test_C9_sonarr_200_with_error_object_must_not_stop_radarr_holds(self):
        e = thg.Env([thg.tor("a", "A", 1)], {"a": ["A/a.mkv"]}, [thg.movie(1, "a.mkv")], sonarr_series={"message": "Database is locked"})
        rc = "CRASH"
        try:
            rc, st, out = e.run()
        except Exception as ex:      # noqa: BLE001
            rc = f"{type(ex).__name__}: {ex}"
        self.assertEqual(e.puts, [("radarr", "movie/1", False)], f"rc={rc!r}: nothing held because the Sonarr wrong-shape reply crashed the run")


class OrphanC(unittest.TestCase):
    def test_C10_failed_quarantine_is_reported_as_deleted(self):
        D = "/mnt/user/Media/Movies/"
        on = {D + f"k{i}.mkv": (10**9, NOW - 10 * 86400) for i in range(9)}
        on[D + "orphan.mkv"] = (10**9, NOW - 10 * 86400)
        known = {p for p in on if "orphan" not in p}
        logs, pruned = [], []
        with mock.patch.object(os_, "load_qbit_known", return_value={"q"}), mock.patch.object(os_, "load_plex_known", return_value=known), \
                mock.patch.object(os_, "load_arr_known", return_value={"a"}), mock.patch.object(os_, "scan_disk", return_value=on), \
                mock.patch.object(os_.safe_ops, "raw_copies", side_effect=lambda p: [p.replace("/mnt/user/", "/mnt/disk1/")]), \
                mock.patch.object(os_.safe_ops, "file_identity", return_value=(1, 1, 1)), \
                mock.patch.object(os_.safe_ops, "unlink_raw", side_effect=lambda paths, *a: [(p, "failed: [Errno 18] cross-device") for p in paths]), \
                mock.patch.object(os_.safe_ops, "prune_empty_parents", side_effect=lambda r, *a, **k: pruned.append(list(r)) or []), \
                mock.patch.object(os_, "log", side_effect=logs.append), mock.patch.object(sys, "argv", ["x", "--apply"]):
            os_.main()
        self.assertTrue(any("APPLY RUN: deleted 0" in l for l in logs), f"quarantine failed but the run log says: {[l for l in logs if 'APPLY RUN' in l]}")

    def test_C11_torrent_files_endpoint_failure_makes_its_files_look_orphaned(self):
        sess = mock.MagicMock()
        login = mock.MagicMock(status_code=200, text="Ok.")
        info = mock.MagicMock(); info.json.return_value = [{"hash": "h", "content_path": "/movies/Foo", "save_path": "/movies"}]
        files = mock.MagicMock(ok=False, status_code=503)
        sess.post.return_value = login
        sess.get.side_effect = lambda url, **kw: info if url.endswith("/torrents/info") else files
        f = "/mnt/user/Media/Movies/Foo/foo.mkv"
        with mock.patch.object(os_.requests, "Session", return_value=sess):
            try:
                known = os_.load_qbit_known()
            except SystemExit:
                return      # failing closed is the correct behaviour
        self.assertEqual(os_.find_orphans({f: (10**9, 0)}, set(), known, {"x"}), {}, "a seeding torrent's file is an orphan candidate after a transient /torrents/files error")

    def test_C12_refusal_exits_zero_when_run_as_a_script(self):
        import runpy
        sess = mock.MagicMock()
        sess.post.return_value = mock.MagicMock(status_code=200, text="Ok.")
        info = mock.MagicMock(); info.json.return_value = [{"hash": "h", "content_path": "/movies/x", "save_path": "/movies"}]
        files = mock.MagicMock(ok=True); files.json.return_value = [{'name': 'x/x.mkv'}]
        sess.get.side_effect = lambda url, **kw: info if url.endswith('/torrents/info') else files
        con = mock.MagicMock(); con.cursor.return_value.fetchall.return_value = []
        import requests as real_requests
        real_open = open

        def fake_open(p, *a, **k):
            if str(p).startswith("/logs"):
                return io.StringIO()
            return real_open(p, *a, **k)
        env = {"RADARR_KEY": "k", "SONARR_KEY": "k"}
        code = None
        with mock.patch.dict(os.environ, env), mock.patch.object(real_requests, "Session", return_value=sess), mock.patch("sqlite3.connect", return_value=con), \
                mock.patch("os.walk", return_value=[]), mock.patch("os.makedirs"), mock.patch("builtins.open", fake_open), \
                mock.patch.object(real_requests, "get", return_value=mock.MagicMock(json=lambda: [])), redirect_stdout(io.StringIO()):
            try:
                runpy.run_path(os.path.join(HERE, "orphan_scan.py"), run_name="__main__")
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertNotIn(code, (0, None), "orphan_scan printed 'Refusing to continue' (main returned 1) but the process exit code is 0")

    def test_C13_docstring_claims_report_only_never_deletes(self):
        self.assertNotIn("Never deletes", os_.__doc__)


class QuarantineC(unittest.TestCase):
    def setUp(self):
        self.mnt = tempfile.mkdtemp()
        p = mock.patch.object(q, "MNT", self.mnt); p.start(); self.addCleanup(p.stop)

    def mk(self, disk, day, rel="Movies/a.mkv"):
        p = os.path.join(self.mnt, disk, "Media", ".quarantine", day, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write("x")
        return p

    def test_C14_one_bad_day_folder_aborts_the_whole_purge(self):
        bad = os.path.join(self.mnt, "disk1", "Media", ".quarantine", "2026-13-45"); os.makedirs(bad)
        good = self.mk("disk2", "2026-01-01")
        try:
            q.purge(7, True, today=datetime.date(2026, 10, 22))
        except Exception:
            pass
        self.assertFalse(os.path.exists(good), "an invalid-date folder name (matches the regex) crashes purge and leaves every other old day folder in place")

    def test_C15_symlinked_day_folder_aborts_purge_of_later_folders(self):
        target = tempfile.mkdtemp(); open(os.path.join(target, "precious"), "w").write("x")
        qd = os.path.join(self.mnt, "disk1", "Media", ".quarantine"); os.makedirs(qd)
        os.symlink(target, os.path.join(qd, "2026-01-01"))
        good = self.mk("disk1", "2026-01-02")
        try:
            q.purge(7, True, today=datetime.date(2026, 10, 22))
        except Exception:
            pass
        self.assertTrue(os.path.exists(os.path.join(target, "precious")))
        self.assertFalse(os.path.exists(good), "a symlink named like a day folder makes rmtree raise and aborts the purge of every later folder")

    def test_C16_retention_is_a_full_N_days(self):
        p = self.mk("disk1", "2026-10-15")
        q.purge(7, True, today=datetime.date(2026, 10, 22))     # folder dated 7 days ago: the files may have been quarantined 6 days 0 min ago
        self.assertTrue(os.path.exists(p), "'older than 7 days' purges a folder exactly 7 calendar days old (up to 23 h 59 m short of a week)")

    def test_C17_purge_wrapper_swallows_failure_exit_code(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "bin")); os.makedirs(os.path.join(d, "scripts", "logs"))
        open(os.path.join(d, "bin", "docker"), "w").write("#!/bin/bash\necho boom\nexit 3\n"); os.chmod(os.path.join(d, "bin", "docker"), 0o755)
        src = open(os.path.join(HERE, "mothership_quarantine_purge.sh")).read().replace("/mnt/user/system/scripts/mothership", os.path.join(d, "scripts"))
        sp = os.path.join(d, "purge.sh"); open(sp, "w").write(src)
        r = subprocess.run(["bash", sp], env=dict(os.environ, PATH=os.path.join(d, "bin") + ":" + os.environ["PATH"]), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "docker/purge failed (exit 3) but the scheduled script exits 0")


if __name__ == "__main__":
    unittest.main()
