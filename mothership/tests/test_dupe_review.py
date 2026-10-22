import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for m in ("requests", "rich", "rich.console", "rich.table", "rich.progress", "rich.panel", "rich.prompt", "rich.box"):
    sys.modules.setdefault(m, mock.MagicMock())
import dupe_review as dr  # noqa: E402


def rec(path="/mnt/user/Media/Movies/A/a.mkv", dur=7_200_000, size=10, **kw):
    r = dr.FileRecord(group_key="movie:1", group_title="A", group_kind="movie", media_item_id=1, metadata_item_id=1, path=path, size=size,
                      width=1920, height=1080, bitrate=0, duration_ms=dur, video_codec="h264", plex_audio_codec="ac3", plex_audio_channels=6,
                      container="mkv", color_trc="")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class MisGroupGuards(unittest.TestCase):
    def test_different_runtimes_are_flagged_and_never_suggested(self):
        a, b = rec(dur=7_200_000), rec("/mnt/user/Media/Movies/A/b.mkv", dur=5_400_000)
        self.assertTrue(dr.durations_mismatch([a, b]))
        self.assertIsNone(dr.suggest_index([a, b]))

    def test_small_runtime_difference_is_the_same_content(self):
        self.assertFalse(dr.durations_mismatch([rec(dur=7_200_000), rec(dur=7_210_000)]))

    def test_discs_of_one_release_are_not_alternate_versions(self):
        a, b = rec("/x/Movie.Disc1.mkv"), rec("/x/Movie.Disc2.mkv")
        self.assertTrue(dr.multi_part_mismatch([a, b]))
        self.assertIsNone(dr.suggest_index([a, b]))

    def test_suggestion_prefers_resolution_then_size(self):
        small, big = rec(size=5), rec("/x/b.mkv", size=9)
        big.height = 2160
        self.assertEqual(dr.suggest_index([small, big]), 1)


class CrossSeedAndKeeper(unittest.TestCase):
    def test_two_torrents_on_one_path_are_both_seen(self):
        idx = {"/mnt/user/Media/arr/movies/X/x.mkv": ["old", "young"]}
        self.assertEqual(dr.torrent_hashes_for("/mnt/user/Media/arr/movies/X/x.mkv", idx), ["old", "young"])
        self.assertEqual(dr.torrent_hashes_for("/mnt/user/Media/arr/movies/X/x.mkv/part", idx), ["old", "young"])
        self.assertEqual(dr.torrent_hashes_for("/mnt/user/Media/arr/movies/XY/x.mkv", idx), [])

    def test_torrent_for_lets_the_youngest_cross_seed_decide_and_counts_the_rest(self):
        idx = {"/mnt/user/Media/Movies/X/x.mkv": ["old", "young"]}
        tors = {"old": {"name": "o", "seeding_time": 200 * 86400}, "young": {"name": "y", "seeding_time": 3 * 86400}}
        r = SimpleNamespace(path="/mnt/user/Media/Movies/X/x.mkv", raw=["/mnt/disk1/Media/Movies/X/x.mkv"], nlink=1)
        with mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_INDEX", idx), mock.patch.object(dr, "QBIT_TORRENTS", tors), mock.patch.object(dr, "qbit_n_files", return_value=1):
            t = dr.torrent_for(r)
        self.assertEqual((t.hash, t.shared_with), ("young", 1))
        self.assertEqual(dr.classify_removal(r.raw, t).mode, "TAG_ONLY")    # young: tagged, never deleted

    def test_empty_file_list_is_unknown_not_zero_files(self):
        sess = mock.MagicMock(); sess.get.return_value.json.return_value = []
        with mock.patch.object(dr, "QBIT_SESSION", sess):
            self.assertEqual(dr.qbit_n_files("h"), 999)

    def test_keeper_in_the_same_torrent_vetoes_a_torrent_delete(self):
        d = tempfile.mkdtemp(); keep = os.path.join(d, "k.mkv"); open(keep, "w").write("x")
        k = SimpleNamespace(path="/mnt/user/Media/Movies/X/k.mkv", raw=[keep])
        t = dr.TorrentInfo("h", "n", 200)
        dec = dr.classify_removal(["/mnt/disk1/Media/Movies/X/l.mkv"], t)
        self.assertTrue(dec.delete_torrent)
        with mock.patch.object(dr, "QBIT_INDEX", {"/mnt/user/Media/Movies/X": ["h"]}):
            self.assertEqual(dr.veto(dec, t, [k]).mode, "REFUSE")
        with mock.patch.object(dr, "QBIT_INDEX", {}):
            self.assertEqual(dr.veto(dec, t, [k]).mode, dec.mode)

    def test_missing_or_symlink_keeper_vetoes_everything(self):
        d = tempfile.mkdtemp(); real = os.path.join(d, "r.mkv"); open(real, "w").write("x"); link = os.path.join(d, "l.mkv"); os.symlink(real, link)
        dec = dr.classify_removal(["/mnt/disk1/Media/Movies/X/l.mkv"], None)
        for raw in ([os.path.join(d, "gone.mkv")], [link]):
            self.assertEqual(dr.veto(dec, None, [SimpleNamespace(path="/p", raw=raw)]).mode, "REFUSE")

    def test_unexplained_hardlinks_are_refused(self):
        r = SimpleNamespace(path="/mnt/user/Media/Movies/X/x.mkv", raw=["/mnt/disk1/Media/Movies/X/x.mkv"], nlink=3)
        with mock.patch.object(dr.safe_ops, "other_names", return_value=["/mnt/disk1/Media/arr/movies/x.mkv"]):
            self.assertIn("another name", dr.hardlink_problem(r))
        with mock.patch.object(dr.safe_ops, "other_names", return_value=["/a", "/b"]):
            self.assertEqual(dr.hardlink_problem(r), "")


class IndexLoading(unittest.TestCase):
    def sess(self, torrents, files_ok=True):
        s = mock.MagicMock()
        s.post.return_value.status_code = 200; s.post.return_value.text = "Ok."
        def get(url, params=None, timeout=None):
            r = mock.MagicMock()
            if url.endswith("/torrents/info"):
                r.json.return_value = torrents
            else:
                r.ok = files_ok; r.json.return_value = [{"name": "f.mkv"}]
            return r
        s.get.side_effect = get
        return s

    def load(self, torrents, **kw):
        with mock.patch.object(dr.requests, "Session", return_value=self.sess(torrents, **kw)), mock.patch.object(dr, "QBIT_INDEX", {}):
            idx = dr.load_qbit_index()
        return idx

    def test_unknown_mount_is_counted_not_silently_dropped(self):
        self.load([{"hash": "h1", "content_path": "/data/downloads/x", "save_path": "/data/downloads"}])
        self.assertEqual(dr.QBIT_SKIPPED, 1)

    def test_music_and_other_non_library_roots_are_not_counted_as_unindexable(self):
        self.load([{"hash": "m1", "content_path": "/music/Album", "save_path": "/music"}, {"hash": "m2", "content_path": "/stash/x", "save_path": "/stash"}])
        self.assertEqual(dr.QBIT_SKIPPED, 0)

    def test_known_torrents_are_indexed_as_lists(self):
        idx = self.load([{"hash": "h1", "content_path": "/movies/X (2020)", "save_path": "/movies"}, {"hash": "h2", "content_path": "/movies/X (2020)", "save_path": "/movies"}])
        self.assertEqual(idx["/mnt/user/Media/Movies/X (2020)"], ["h1", "h2"]); self.assertEqual(dr.QBIT_SKIPPED, 0)

    def test_rootless_torrent_with_failing_files_api_is_counted(self):
        self.load([{"hash": "h1", "content_path": "/movies", "save_path": "/movies"}], files_ok=False)
        self.assertEqual(dr.QBIT_SKIPPED, 1)


class ExecutePlan(unittest.TestCase):
    def plan(self, **kw):
        a = dict(title="A", kind="movie", loser="/mnt/user/Media/Movies/A/lose.mkv", keep=["/mnt/user/Media/Movies/A/keep.mkv"], mode="UNLINK",
                 torrent="", arr="", arr_id=0, size=1)
        a.update(kw)
        p = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump([a], p)
        p.close()
        return p.name

    def run_plan(self, path, raw_map, torrent=None, apply=True):
        executed = []
        with mock.patch.object(dr, "APPLY", apply), mock.patch.object(dr, "load_qbit_index"), mock.patch.object(dr, "raw_copies", side_effect=lambda p: raw_map.get(p, [])), \
                mock.patch.object(dr, "torrent_for", return_value=torrent), mock.patch.object(dr, "plex_refresh_and_empty_trash"), \
                mock.patch.object(dr.safe_ops, "execute", side_effect=lambda dec, *a, **k: executed.append(dec.mode) or []), \
                mock.patch.object(dr.safe_ops, "file_identity", return_value=(1, 1, 1)), mock.patch.object(dr.os.path, "exists", return_value=True), mock.patch.object(dr.os.path, "getsize", return_value=1), \
                mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_TORRENTS", {"x": {}}), mock.patch.object(dr, "QBIT_SKIPPED", 0), \
                mock.patch.object(dr.safe_ops, "keeper_problem", return_value=""), mock.patch.object(dr, "hardlink_problem", return_value=""):
            dr.execute_plan(path)
        return executed

    RAW = {"/mnt/user/Media/Movies/A/lose.mkv": ["/mnt/disk1/Media/Movies/A/lose.mkv"], "/mnt/user/Media/Movies/A/keep.mkv": ["/mnt/disk1/Media/Movies/A/keep.mkv"]}

    def test_valid_plan_runs(self):
        self.assertEqual(self.run_plan(self.plan(), self.RAW), ["UNLINK"])

    def test_missing_keeper_removes_nothing(self):
        raw = dict(self.RAW); raw["/mnt/user/Media/Movies/A/keep.mkv"] = []
        self.assertEqual(self.run_plan(self.plan(), raw), [])

    def test_state_change_since_planning_is_skipped(self):
        # planned as UNLINK, but a young torrent now owns the file -> classification differs -> skipped, not guessed
        t = dr.TorrentInfo("h", "n", seed_days=1, covers_exact_path=True)
        self.assertEqual(self.run_plan(self.plan(mode="UNLINK"), self.RAW, torrent=t), [])

    def test_loser_on_two_disks_is_refused(self):
        raw = dict(self.RAW); raw["/mnt/user/Media/Movies/A/lose.mkv"] = ["/mnt/disk1/x", "/mnt/disk2/x"]
        self.assertEqual(self.run_plan(self.plan(), raw), [])

    def test_apply_without_qbit_is_refused_not_silently_unsafe(self):
        with mock.patch.object(dr, "APPLY", True), mock.patch.object(dr, "QBIT_SESSION", None):
            with self.assertRaises(SystemExit):
                dr.require_qbit_for_apply()
        with mock.patch.object(dr, "APPLY", False), mock.patch.object(dr, "QBIT_SESSION", None):
            dr.require_qbit_for_apply()   # dry runs may proceed

    def test_plan_naming_a_path_outside_the_libraries_is_skipped(self):
        p = self.plan(loser="/mnt/user/appdata/x.mkv")
        raw = dict(self.RAW); raw["/mnt/user/appdata/x.mkv"] = ["/mnt/disk1/appdata/x.mkv"]
        self.assertEqual(self.run_plan(p, raw), [])

    def test_plan_with_a_different_torrent_now_is_skipped(self):
        t = dr.TorrentInfo("newhash", "n", seed_days=100, covers_exact_path=True)      # planned as UNLINK with no torrent; now a seasoned torrent owns it
        self.assertEqual(self.run_plan(self.plan(mode="QBIT_DELETE", torrent="oldhash"), self.RAW, torrent=t), [])

    def test_apply_refused_when_index_is_incomplete(self):
        with mock.patch.object(dr, "APPLY", True), mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_TORRENTS", {"x": {}}), mock.patch.object(dr, "QBIT_SKIPPED", 2):
            with self.assertRaises(SystemExit):
                dr.require_qbit_for_apply()
        with mock.patch.object(dr, "APPLY", True), mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_TORRENTS", {}), mock.patch.object(dr, "QBIT_SKIPPED", 0):
            with self.assertRaises(SystemExit):
                dr.require_qbit_for_apply()

    def test_loser_equal_to_keeper_is_refused(self):
        raw = dict(self.RAW); raw["/mnt/user/Media/Movies/A/lose.mkv"] = raw["/mnt/user/Media/Movies/A/keep.mkv"]
        self.assertEqual(self.run_plan(self.plan(), raw), [])


if __name__ == "__main__":
    unittest.main()
