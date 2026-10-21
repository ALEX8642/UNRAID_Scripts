import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("requests", mock.MagicMock())
import seed_cleanup as sc  # noqa: E402

NOW = time.time()


def torrent(h, name, **kw):
    t = dict(hash=h, name=name, state="missingFiles", content_path=f"/movies/{name}", save_path="/movies", added_on=NOW - 40 * 86400,
             completion_on=NOW - 39 * 86400, ratio=0.2, seeding_time=0)
    t.update(kw)
    return t


class FakeSession:
    def __init__(self, torrents, files):
        self.torrents, self.files, self.deleted = torrents, files, []

    def post(self, url, data=None, **kw):
        r = mock.MagicMock()
        if url.endswith("/auth/login"):
            r.text = "Ok."
        elif url.endswith("/torrents/delete"):
            assert data["deleteFiles"] == "false", "seed_cleanup must never delete files"
            self.deleted.append(data["hashes"])
        return r

    def get(self, url, params=None, **kw):
        r = mock.MagicMock()
        r.json.return_value = self.torrents if url.endswith("/torrents/info") else self.files[params["hash"]]
        return r


def run(torrents, deleted_names, apply=True, on_array=False, state=None, disks=3, saved=None):
    files = {t["hash"]: [{"name": t["name"] + "/" + t["name"] + ".mkv", "size": 5}] for t in torrents}
    fs = FakeSession(torrents, files)
    st = {} if state is None else state
    saved = [] if saved is None else saved
    out = io.StringIO()
    with mock.patch.object(sc.requests, "Session", return_value=fs), mock.patch.object(sc, "exists_on_array", return_value=on_array), \
            mock.patch.object(sc, "upgrade_deleted_names", return_value=deleted_names), mock.patch("os.path.isdir", return_value=True), \
            mock.patch.object(sc, "load_state", return_value=st), mock.patch.object(sc, "save_state", side_effect=lambda s: saved.append(dict(s))), \
            mock.patch.object(sc, "mounted_disks", return_value=disks), mock.patch.dict(os.environ, {"SEED_CLEANUP_LOCK": os.path.join(__import__("tempfile").mkdtemp(), "lock")}), \
            mock.patch.object(sys, "argv", ["x"] + (["--apply"] if apply else [])), redirect_stdout(out):
        sc.main()
    return fs.deleted, out.getvalue()


OLD = {"a": NOW - 30 * 3600}
UP = {"Movie.A.mkv": NOW - 20 * 86400}      # upgrade deletion record, dated after the torrent completed (39 days ago)


class SeedCleanup(unittest.TestCase):
    def test_removes_after_second_sighting(self):
        d, _ = run([torrent("a", "Movie.A")], UP, state=dict(OLD))
        self.assertEqual(d, ["a"])

    def test_first_sighting_waits_and_is_recorded_only_on_apply(self):
        saved = []
        d, out = run([torrent("a", "Movie.A")], UP, state={}, saved=saved)
        self.assertEqual(d, []); self.assertIn("second sighting", out); self.assertIn("a", saved[0])
        saved2 = []
        run([torrent("a", "Movie.A")], UP, state={}, apply=False, saved=saved2)
        self.assertEqual(saved2, [])                  # a dry run must not advance the strike clock

    def test_content_still_on_array_is_kept(self):
        d, _ = run([torrent("a", "Movie.A")], UP, on_array=True, state=dict(OLD))
        self.assertEqual(d, [])

    def test_no_upgrade_record_is_kept(self):
        d, out = run([torrent("a", "Movie.A")], {}, state=dict(OLD))
        self.assertEqual(d, []); self.assertIn("no Radarr/Sonarr upgrade-deletion record", out)

    def test_upgrade_record_older_than_the_torrent_proves_nothing(self):
        d, out = run([torrent("a", "Movie.A", completion_on=NOW - 5 * 86400)], {"Movie.A.mkv": NOW - 90 * 86400}, state=dict(OLD))
        self.assertEqual(d, []); self.assertIn("predates", out)

    def test_seeding_rule_not_met_waits(self):
        t = torrent("a", "Movie.A", completion_on=NOW - 2 * 86400, added_on=NOW - 2 * 86400)
        d, out = run([t], {"Movie.A.mkv": NOW - 86400}, state=dict(OLD))
        self.assertEqual(d, []); self.assertIn("not met", out)

    def test_never_completed_torrent_is_not_ancient(self):
        for done in (-1, 0):
            d, out = run([torrent("a", "Movie.A", completion_on=done)], UP, state=dict(OLD))
            self.assertEqual(d, [], done)

    def test_wall_clock_age_alone_does_not_satisfy_the_rule_for_a_torrent_that_could_not_seed(self):
        # completed 6 days ago, deleted by an upgrade 3 hours later, now missingFiles: it never seeded, so 6 days of wall time proves nothing
        t = torrent("a", "Movie.A", completion_on=NOW - 6 * 86400, added_on=NOW - 6 * 86400)
        d, out = run([t], {"Movie.A.mkv": NOW - 6 * 86400 + 3 * 3600}, state=dict(OLD))
        self.assertEqual(d, [])

    def test_ratio_or_real_seeding_time_satisfies_rule(self):
        t = torrent("a", "Movie.A", completion_on=NOW - 2 * 86400, added_on=NOW - 2 * 86400, ratio=1.1)
        self.assertEqual(run([t], {"Movie.A.mkv": NOW - 86400}, state=dict(OLD))[0], ["a"])
        t = torrent("a", "Movie.A", completion_on=NOW - 2 * 86400, added_on=NOW - 2 * 86400, seeding_time=130 * 3600)
        self.assertEqual(run([t], {"Movie.A.mkv": NOW - 86400}, state=dict(OLD))[0], ["a"])

    def test_circuit_breaker(self):
        ts = [torrent(h, f"M{h}") for h in "abcdefg"]
        d, out = run(ts, {f"M{h}.mkv": NOW - 20 * 86400 for h in "abcdefg"}, state={h: NOW - 30 * 3600 for h in "abcdefg"})
        self.assertEqual(d, []); self.assertIn("CIRCUIT BREAKER", out)

    def test_dry_run_removes_nothing(self):
        d, out = run([torrent("a", "Movie.A")], UP, apply=False, state=dict(OLD))
        self.assertEqual(d, []); self.assertIn("WOULD REMOVE", out)

    def test_a_missing_data_disk_stops_the_run(self):
        with self.assertRaises(SystemExit):
            run([torrent("a", "Movie.A")], UP, state={**OLD, "_disks": 12}, disks=11)

    def test_state_is_saved_before_the_delete(self):
        order = []
        saved = []
        files = {"a": [{"name": "Movie.A/Movie.A.mkv", "size": 5}]}
        fs = FakeSession([torrent("a", "Movie.A")], files)
        orig = fs.post
        fs.post = lambda url, data=None, **kw: (order.append("delete") if url.endswith("/torrents/delete") else None) or orig(url, data, **kw)
        with mock.patch.object(sc.requests, "Session", return_value=fs), mock.patch.object(sc, "exists_on_array", return_value=False), \
                mock.patch.object(sc, "upgrade_deleted_names", return_value=UP), mock.patch.object(sc, "load_state", return_value=dict(OLD)), \
                mock.patch.object(sc, "save_state", side_effect=lambda s: order.append("save")), mock.patch.object(sc, "mounted_disks", return_value=3), \
                mock.patch.dict(os.environ, {"SEED_CLEANUP_LOCK": os.path.join(__import__("tempfile").mkdtemp(), "lock")}), mock.patch("os.path.isdir", return_value=True), \
                mock.patch.object(sys, "argv", ["x", "--apply"]), redirect_stdout(io.StringIO()):
            sc.main()
        self.assertEqual(order[:2], ["save", "delete"])


class ContentPresent(unittest.TestCase):
    def test_directory_exists_but_own_files_gone(self):
        t = torrent("a", "Show.S01", content_path="/tv/Show.S01", save_path="/tv")
        fs = FakeSession([t], {"a": [{"name": "Show.S01/old.mkv", "size": 5 * 10**8}]})
        with mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p == "/tv/Show.S01"):
            self.assertFalse(sc.content_present(fs, t))
        with mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p == "/tv/Show.S01/old.mkv"):
            self.assertTrue(sc.content_present(fs, t))

    def test_a_leftover_nfo_does_not_count_as_content(self):
        t = torrent("a", "M", save_path="/movies")
        fs = FakeSession([t], {"a": [{"name": "M/M.mkv", "size": 20 * 10**9}, {"name": "M/M.nfo", "size": 3000}]})
        with mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p.endswith(".nfo")):
            self.assertFalse(sc.content_present(fs, t))


class Paths(unittest.TestCase):
    def test_candidates_include_the_share_and_other_pools(self):
        with mock.patch.object(sc.os, "listdir", return_value=["disk1", "cache", "nvme", "user", "user0", "disks"]), mock.patch.object(sc.os.path, "isdir", return_value=True):
            c = sc.raw_candidates("/movies/A/a.mkv")
        self.assertIn("/mnt/user/Media/Movies/A/a.mkv", c); self.assertIn("/mnt/nvme/Media/Movies/A/a.mkv", c)
        self.assertNotIn("/mnt/disks/Media/Movies/A/a.mkv", c)

    def test_unknown_location_is_treated_as_present(self):
        self.assertTrue(sc.exists_on_array("/somewhere/else/x.mkv"))

    def test_upgrade_history_filters_reason_and_keeps_earliest_date(self):
        recs = {"records": [{"data": {"reason": "Upgrade"}, "sourceTitle": "/m/A/a.mkv", "date": "2026-10-02T10:00:00Z"},
                            {"data": {"reason": "Upgrade"}, "sourceTitle": "/m/B/a.mkv", "date": "2026-10-01T10:00:00Z"},
                            {"data": {"reason": "Manual"}, "sourceTitle": "/m/A/b.mkv", "date": "2026-10-03T10:00:00Z"}]}
        resp = mock.MagicMock(); resp.json.side_effect = [recs, {"records": []}]
        with mock.patch.object(sc.requests, "get", return_value=resp):
            names = sc.upgrade_deleted_names("radarr")
        self.assertEqual(set(names), {"a.mkv"}); self.assertEqual(names["a.mkv"], sc._ts("2026-10-01T10:00:00Z"))


if __name__ == "__main__":
    unittest.main()
