import json
import os
import sys
import tempfile
import unittest
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
                mock.patch.object(dr.safe_ops, "file_identity", return_value=(1, 1, 1)), mock.patch.object(dr.os.path, "exists", return_value=True), \
                mock.patch.object(dr, "QBIT_SESSION", object()):
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

    def test_loser_equal_to_keeper_is_refused(self):
        raw = dict(self.RAW); raw["/mnt/user/Media/Movies/A/lose.mkv"] = raw["/mnt/user/Media/Movies/A/keep.mkv"]
        self.assertEqual(self.run_plan(self.plan(), raw), [])


if __name__ == "__main__":
    unittest.main()
