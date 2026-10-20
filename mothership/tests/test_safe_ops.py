import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import safe_ops as so
from safe_ops import TorrentInfo, classify_removal, execute, raw_copies
import common


class FakeSession:
    def __init__(self): self.calls = []
    def post(self, url, data=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], dict(data or {})))
        class R: ok = True; status_code = 200
        return R()


class PathMapTests(unittest.TestCase):
    def test_media_paths_map_to_user_share(self):
        self.assertEqual(common.to_host_path("/media/Movies/A (2020)/a.mkv"), ("/mnt/user/Media/Movies/A (2020)/a.mkv", True))
        self.assertEqual(common.to_host_path("/media/tv/S/Season 1/e.mkv"), ("/mnt/user/Media/tv/S/Season 1/e.mkv", True))
        self.assertEqual(common.to_host_path("/movies/x.mkv")[1], True)       # old Plex/qBit mapping still works
        self.assertEqual(common.to_host_path("/nowhere/x.mkv")[1], False)


class RawCopyTests(unittest.TestCase):
    def test_resolves_every_physical_copy(self):
        with tempfile.TemporaryDirectory() as d:
            for disk in ("disk1", "disk2"):
                os.makedirs(f"{d}/{disk}/Media/Movies/A"); open(f"{d}/{disk}/Media/Movies/A/a.mkv", "w").close()
            os.makedirs(f"{d}/disk3/Media/Movies/A")
            hits = raw_copies("/mnt/user/Media/Movies/A/a.mkv", disk_glob=f"{d}/disk[0-9]*")
            self.assertEqual([h.split(d)[1] for h in hits], ["/disk1/Media/Movies/A/a.mkv", "/disk2/Media/Movies/A/a.mkv"])
            self.assertEqual(raw_copies("/not/user/path", disk_glob=f"{d}/disk[0-9]*"), [])


class ClassifyTests(unittest.TestCase):
    RAW = ["/mnt/disk1/Media/Movies/A/a.mkv"]

    def T(self, **kw):
        base = dict(hash="h", name="A.2020.1080p", seed_days=100, n_files=1, covers_exact_path=True); base.update(kw)
        return TorrentInfo(**base)

    def test_no_torrent_unlinks_physical_copy(self):
        d = classify_removal(self.RAW, None); self.assertEqual(d.mode, "UNLINK"); self.assertEqual(d.unlink_raw, self.RAW)

    def test_unresolvable_or_multi_disk_is_refused(self):
        self.assertEqual(classify_removal([], None).mode, "REFUSE")
        self.assertEqual(classify_removal(self.RAW + ["/mnt/disk2/Media/Movies/A/a.mkv"], None).mode, "REFUSE")

    def test_keeper_is_never_removed(self):
        self.assertEqual(classify_removal(self.RAW, None, keeper_raw=self.RAW[0]).mode, "REFUSE")

    def test_pack_torrent_is_refused(self):
        d = classify_removal(self.RAW, self.T(n_files=20)); self.assertEqual(d.mode, "REFUSE"); self.assertFalse(d.unlink_raw)

    def test_own_name_seasoned_goes_through_qbit_not_unlink(self):
        d = classify_removal(self.RAW, self.T(seed_days=100))
        self.assertEqual(d.mode, "QBIT_DELETE"); self.assertTrue(d.delete_torrent); self.assertFalse(d.unlink_raw)

    def test_own_name_young_is_tag_only_and_touches_no_file(self):
        d = classify_removal(self.RAW, self.T(seed_days=3))
        self.assertEqual(d.mode, "TAG_ONLY"); self.assertTrue(d.tag); self.assertFalse(d.unlink_raw); self.assertFalse(d.delete_torrent)

    def test_hardlinked_library_name_seasoned_unlinks_then_deletes_torrent(self):
        d = classify_removal(self.RAW, self.T(covers_exact_path=False, seed_days=100))
        self.assertEqual(d.mode, "LIBRARY_NAME_THEN_DELETE"); self.assertEqual(d.unlink_raw, self.RAW); self.assertTrue(d.delete_torrent)

    def test_hardlinked_library_name_young_unlinks_and_tags_only(self):
        d = classify_removal(self.RAW, self.T(covers_exact_path=False, seed_days=1.3))
        self.assertEqual(d.mode, "LIBRARY_NAME_THEN_TAG"); self.assertEqual(d.unlink_raw, self.RAW); self.assertTrue(d.tag); self.assertFalse(d.delete_torrent)

    def test_threshold_is_configurable(self):
        self.assertEqual(classify_removal(self.RAW, self.T(seed_days=10), hnr_days=5).mode, "QBIT_DELETE")
        self.assertEqual(classify_removal(self.RAW, self.T(seed_days=10), hnr_days=30).mode, "TAG_ONLY")


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.f = os.path.join(self.d, "a.mkv")
        with open(self.f, "w") as fh: fh.write("x")

    def test_dry_run_changes_nothing_and_calls_nothing(self):
        s = FakeSession(); dec = classify_removal([self.f], TorrentInfo("h", "n", 100, covers_exact_path=False))
        out = execute(dec, s, "http://q", apply=False)
        self.assertTrue(os.path.exists(self.f)); self.assertEqual(s.calls, []); self.assertTrue(any("would" in o for o in out))

    def test_apply_unlinks_library_name_before_deleting_torrent(self):
        events = []
        class S(FakeSession):
            def post(self, url, data=None, timeout=None):
                events.append(("api", os.path.exists(self.f), url.rsplit("/", 1)[-1])); return super().post(url, data, timeout)
        s = S(); s.f = self.f
        dec = classify_removal([self.f], TorrentInfo("h", "n", 100, covers_exact_path=False))
        execute(dec, s, "http://q", apply=True)
        self.assertFalse(os.path.exists(self.f))
        self.assertEqual(events, [("api", False, "delete")])   # file already gone when the torrent is deleted
        self.assertEqual(s.calls[0][1]["deleteFiles"], "true")

    def test_tag_only_never_deletes_files(self):
        s = FakeSession(); dec = classify_removal([self.f], TorrentInfo("h", "n", 2))
        execute(dec, s, "http://q", apply=True)
        self.assertTrue(os.path.exists(self.f)); self.assertEqual([c[0] for c in s.calls], ["addTags"]); self.assertEqual(s.calls[0][1]["tags"], "delete-later")

    def test_fuse_paths_are_never_unlinked(self):
        out = so.unlink_raw(["/mnt/user/Media/Movies/x.mkv"], apply=True); self.assertEqual(out[0][1], "refused: FUSE path")


if __name__ == "__main__":
    unittest.main()
