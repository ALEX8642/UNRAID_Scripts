import os, sys, tempfile, unittest, unittest.mock
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


class OtherNamesTests(unittest.TestCase):
    def test_finds_hardlink_in_arr_tree_only(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/disk1/Media/Movies/A"); os.makedirs(f"{d}/disk1/Media/arr/movies"); os.makedirs(f"{d}/disk1/Media/other")
            lib = f"{d}/disk1/Media/Movies/A/a.mkv"
            with open(lib, "w") as fh: fh.write("x")
            os.link(lib, f"{d}/disk1/Media/arr/movies/seed.mkv"); os.link(lib, f"{d}/disk1/Media/other/ignored.mkv")
            self.assertEqual(so.other_names(lib), [f"{d}/disk1/Media/arr/movies/seed.mkv"])
            solo = f"{d}/disk1/Media/Movies/A/solo.mkv"
            with open(solo, "w") as fh: fh.write("y")
            self.assertEqual(so.other_names(solo), [])


class PruneTests(unittest.TestCase):
    def test_prunes_only_emptied_parents_and_stops_at_library_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = f"{d}/disk1/Media/Movies"
            os.makedirs(f"{root}/Gone (2020)/extras"); os.makedirs(f"{root}/Keep (2021)"); os.makedirs(f"{root}/Placeholder (2022)")
            a = f"{root}/Gone (2020)/extras/x.mkv"; b = f"{root}/Keep (2021)/y.mkv"; c = f"{root}/Keep (2021)/z.mkv"
            for f in (a, b, c):
                with open(f, "w") as fh: fh.write("x")
            os.remove(a); os.remove(b)
            pruned = so.prune_empty_parents([a, b], True)
            self.assertEqual(sorted(p.split("Movies/")[1] for p in pruned), ["Gone (2020)", "Gone (2020)/extras"])
            self.assertTrue(os.path.isdir(f"{root}/Keep (2021)"))          # still holds z.mkv
            self.assertTrue(os.path.isdir(f"{root}/Placeholder (2022)"))   # pre-existing empty folder untouched
            self.assertTrue(os.path.isdir(root))                            # library root never removed


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
        # the file is quarantined first; qBittorrent then forgets the torrent WITHOUT deleting files
        self.assertEqual(d.mode, "QBIT_DELETE"); self.assertTrue(d.delete_torrent); self.assertFalse(d.delete_files); self.assertEqual(d.unlink_raw, self.RAW)

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
        self.d = tempfile.mkdtemp(); os.makedirs(os.path.join(self.d, "Media", "Movies")); self.f = os.path.join(self.d, "Media", "Movies", "a.mkv")
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
        self.assertEqual(events, [("api", False, "delete")])   # file already moved away when the torrent is deleted
        self.assertEqual(s.calls[0][1]["deleteFiles"], "true")  # a hardlinked seed: the data is still held by the quarantined name
        self.assertTrue(any(f == "a.mkv" for _r, _d, fs in os.walk(os.path.join(self.d, "Media", ".quarantine")) for f in fs))

    def test_tag_only_never_deletes_files(self):
        s = FakeSession(); dec = classify_removal([self.f], TorrentInfo("h", "n", 2))
        execute(dec, s, "http://q", apply=True)
        self.assertTrue(os.path.exists(self.f)); self.assertEqual([c[0] for c in s.calls], ["addTags"]); self.assertEqual(s.calls[0][1]["tags"], "delete-later")

    def test_fuse_paths_are_never_unlinked(self):
        out = so.unlink_raw(["/mnt/user/Media/Movies/x.mkv"], apply=True); self.assertEqual(out[0][1], "refused: FUSE path")


class SecondRoundTests(unittest.TestCase):
    def test_glob_metacharacters_in_names_are_literal(self):
        with tempfile.TemporaryDirectory() as d:
            for p in ("Foo [2020]/a.mkv", "Foo 2/a.mkv", "Who!.mkv"):
                os.makedirs(os.path.dirname(f"{d}/disk1/Media/Movies/{p}"), exist_ok=True); open(f"{d}/disk1/Media/Movies/{p}", "w").close()
            g = f"{d}/disk[0-9]*"
            self.assertEqual([h.split("Movies/")[1] for h in raw_copies("/mnt/user/Media/Movies/Foo [2020]/a.mkv", disk_glob=g)], ["Foo [2020]/a.mkv"])
            self.assertEqual(raw_copies("/mnt/user/Media/Movies/Who?.mkv", disk_glob=g), [])      # '?' must not match 'Who!.mkv'
            self.assertEqual(raw_copies("/mnt/user/Media/Movies/Foo *", disk_glob=g), [])

    def test_failed_unlink_aborts_before_touching_the_torrent(self):
        s = FakeSession()
        dec = classify_removal(["/nonexistent/disk1/Media/Movies/A/a.mkv"], TorrentInfo("h", "n", 100, covers_exact_path=False))
        out = execute(dec, s, "http://q", apply=True)
        self.assertEqual(s.calls, [])
        self.assertTrue(any(o.startswith("ABORT") for o in out)); self.assertFalse(so.succeeded(out))

    def test_succeeded_requires_every_line_to_be_a_success(self):
        self.assertTrue(so.succeeded(["quarantined: /x", "qBit deleted"]))
        self.assertTrue(so.succeeded(["would unlink: /x", "qBit would delete torrent+files"]))
        self.assertFalse(so.succeeded(["quarantined: /x", "qBit delete failed HTTP 500"]))
        self.assertFalse(so.succeeded(["missing: /x"])); self.assertFalse(so.succeeded([]))

    def test_shared_torrent_files_are_never_deleted(self):
        d = classify_removal(["/mnt/disk1/Media/Movies/A/a.mkv"], TorrentInfo("h", "n", 200, shared_with=1))
        self.assertEqual(d.mode, "REFUSE"); self.assertFalse(d.delete_torrent)
        # a young shared torrent is only tagged (never deleted), which is safe
        self.assertEqual(classify_removal(["/mnt/disk1/Media/Movies/A/a.mkv"], TorrentInfo("h", "n", 1, shared_with=1)).mode, "TAG_ONLY")

    def test_keeper_must_be_one_real_file(self):
        d = tempfile.mkdtemp(); real = os.path.join(d, "k.mkv"); open(real, "w").write("x")
        link = os.path.join(d, "l.mkv"); os.symlink(real, link); empty = os.path.join(d, "e.mkv"); open(empty, "w").close()
        self.assertEqual(so.keeper_problem([[real]]), "")
        for bad in ([[link]], [[empty]], [[os.path.join(d, "gone.mkv")]], [[real, real]], [[]]):
            self.assertTrue(so.keeper_problem(bad), bad)

    def test_hnr_threshold_cannot_be_zeroed_by_environment(self):
        with unittest.mock.patch.dict(os.environ, {"HNR_MIN_SEED_DAYS": "0"}):
            self.assertGreaterEqual(so._env_days("HNR_MIN_SEED_DAYS", "30", 5.0), 5.0)
        with unittest.mock.patch.dict(os.environ, {"HNR_MIN_SEED_DAYS": "abc"}):
            self.assertEqual(so._env_days("HNR_MIN_SEED_DAYS", "30", 5.0), 30.0)

    def test_seed_rule(self):
        now = 10_000_000
        t = lambda **k: dict(completion_on=now - 3600, ratio=0, seeding_time=0, state="stalledUP", **k)  # noqa: E731
        self.assertFalse(so.seed_rule_met(dict(t(), completion_on=-1), now))          # never completed is not "ancient"
        self.assertFalse(so.seed_rule_met(dict(t(), completion_on=0), now))
        self.assertTrue(so.seed_rule_met(dict(t(), ratio=1.0), now))
        self.assertTrue(so.seed_rule_met(dict(t(), seeding_time=121 * 3600), now))
        self.assertTrue(so.seed_rule_met(dict(t(), completion_on=now - 6 * 86400), now))                     # actively seeding for 6 days
        self.assertFalse(so.seed_rule_met(dict(t(), completion_on=now - 6 * 86400, state="pausedUP"), now))  # paused: wall time is not seeding time
        self.assertFalse(so.seed_rule_met(dict(t(), completion_on=now - 6 * 86400, state="missingFiles"), now))
        self.assertTrue(so.seed_rule_met(dict(t(), completion_on=now - 20 * 86400, state="pausedUP"), now))


class QuarantineTests(unittest.TestCase):
    def tree(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/disk1/Media/Movies/A (2020)"); f = f"{d}/disk1/Media/Movies/A (2020)/a.mkv"
        with open(f, "w") as fh: fh.write("data")
        return d, f

    def test_removal_is_a_same_disk_rename_into_dated_quarantine(self):
        d, f = self.tree()
        out = so.unlink_raw([f], True)
        self.assertEqual(out[0][1], "quarantined"); self.assertFalse(os.path.exists(f))
        q = so.quarantine_path(f.replace("a.mkv", "zz.mkv"), "2026-10-22")
        self.assertIn("/disk1/Media/.quarantine/2026-10-22/Movies/A (2020)/", q)
        found = [os.path.join(r, n) for r, _d, fs in os.walk(f"{d}/disk1/Media/.quarantine") for n in fs]
        self.assertEqual(len(found), 1); self.assertEqual(open(found[0]).read(), "data")           # the bytes are intact and recoverable
        self.assertEqual(os.stat(found[0]).st_dev, os.stat(f"{d}/disk1/Media").st_dev)             # same device: a rename, no copy

    def test_name_collisions_never_overwrite_earlier_quarantine(self):
        d, f = self.tree()
        so.unlink_raw([f], True)
        with open(f, "w") as fh: fh.write("second")
        so.unlink_raw([f], True)
        found = sorted(os.path.join(r, n) for r, _d, fs in os.walk(f"{d}/disk1/Media/.quarantine") for n in fs)
        self.assertEqual(sorted(open(x).read() for x in found), ["data", "second"])

    def test_dry_run_moves_nothing(self):
        d, f = self.tree()
        self.assertEqual(so.unlink_raw([f], False)[0][1], "would quarantine"); self.assertTrue(os.path.exists(f))

    def test_files_outside_media_or_already_quarantined_are_refused(self):
        d = tempfile.mkdtemp(); f = os.path.join(d, "x.mkv"); open(f, "w").close()
        self.assertTrue(so.unlink_raw([f], True)[0][1].startswith("failed")); self.assertTrue(os.path.exists(f))
        with self.assertRaises(ValueError):
            so.quarantine_path("/mnt/disk1/Media/.quarantine/2026-10-22/Movies/a.mkv")

    def test_library_seed_deletion_keeps_files_unless_data_is_reachable_elsewhere(self):
        young_exact = classify_removal(["/mnt/disk1/Media/Movies/A/a.mkv"], TorrentInfo("h", "n", 100, covers_exact_path=True))
        self.assertFalse(young_exact.delete_files)
        arr_seed = classify_removal(["/mnt/disk1/Media/Movies/A/a.mkv"], TorrentInfo("h", "n", 100, covers_exact_path=False))
        self.assertTrue(arr_seed.delete_files)           # only because the quarantined library name still holds the bytes


class HardeningTests(unittest.TestCase):
    def test_raw_copies_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/disk1/Media"); os.makedirs(f"{d}/etc"); open(f"{d}/etc/passwd", "w").close()
            self.assertEqual(raw_copies("/mnt/user/../etc/passwd", disk_glob=f"{d}/disk[0-9]*"), [])
            self.assertEqual(raw_copies("/mnt/user/", disk_glob=f"{d}/disk[0-9]*"), [])

    def test_prune_never_removes_media_arr_or_library_roots(self):
        with tempfile.TemporaryDirectory() as d:
            # a seed folder under arr/ that empties out must NOT be pruned (qBittorrent's save path lives there)
            os.makedirs(f"{d}/disk1/Media/arr/movies/Seed"); f = f"{d}/disk1/Media/arr/movies/Seed/x.mkv"
            open(f, "w").close(); os.remove(f)
            self.assertEqual(so.prune_empty_parents([f], True), [])
            self.assertTrue(os.path.isdir(f"{d}/disk1/Media/arr/movies/Seed"))
            # an emptied library tree stops at the library root, never removing Media itself
            os.makedirs(f"{d}/disk1/Media/Movies/Only (2020)"); g = f"{d}/disk1/Media/Movies/Only (2020)/o.mkv"
            open(g, "w").close(); os.remove(g)
            self.assertEqual(so.prune_empty_parents([g], True), [f"{d}/disk1/Media/Movies/Only (2020)"])
            self.assertTrue(os.path.isdir(f"{d}/disk1/Media/Movies")); self.assertTrue(os.path.isdir(f"{d}/disk1/Media"))

    def test_qbit_failure_after_unlink_is_reported_not_raised(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "Media", "Movies")); f = os.path.join(d, "Media", "Movies", "a.mkv"); open(f, "w").close()
        class Boom:
            def post(self, *a, **k): raise ConnectionError("qbit down")
        dec = classify_removal([f], TorrentInfo("h", "n", 100, covers_exact_path=False))
        out = execute(dec, Boom(), "http://q", apply=True)
        self.assertFalse(os.path.exists(f))                      # library name was handled first
        self.assertTrue(any("FAILED" in o for o in out))

    def test_unlink_refuses_directories_and_missing(self):
        d = tempfile.mkdtemp(); sub = os.path.join(d, "dir"); os.makedirs(sub)
        out = dict(so.unlink_raw([sub, os.path.join(d, "nope.mkv")], apply=True))
        self.assertEqual(out[sub], "missing"); self.assertTrue(os.path.isdir(sub))

    def test_unlink_only_removes_the_name_not_other_hardlinks(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "Media", "Movies")); a = os.path.join(d, "Media", "Movies", "a.mkv"); b = os.path.join(d, "b.mkv")
        
        with open(a, "w") as fh: fh.write("data")
        os.link(a, b)
        so.unlink_raw([a], apply=True)
        self.assertFalse(os.path.exists(a)); self.assertEqual(open(b).read(), "data")

    def test_dry_run_prune_and_unlink_touch_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/disk1/Media/Movies/Gone"); f = f"{d}/disk1/Media/Movies/Gone/g.mkv"; open(f, "w").close(); os.remove(f)
            self.assertEqual(so.prune_empty_parents([f], apply=False), [f"{d}/disk1/Media/Movies/Gone"])
            self.assertTrue(os.path.isdir(f"{d}/disk1/Media/Movies/Gone"))


if __name__ == "__main__":
    unittest.main()
