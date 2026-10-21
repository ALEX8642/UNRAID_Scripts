import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for m in ("requests", "rich", "rich.console", "rich.table", "rich.progress", "rich.box"):
    sys.modules.setdefault(m, mock.MagicMock())
import orphan_scan as os_  # noqa: E402

OLD = time.time() - 10 * 86400
D = "/mnt/user/Media/Movies/"


def disk(*names, mtime=OLD):
    return {D + n: (10**9, mtime) for n in names}


class FindOrphans(unittest.TestCase):
    def test_a_file_known_to_any_one_source_is_never_an_orphan(self):
        on = disk("plex.mkv", "qbit.mkv", "arr.mkv", "orphan.mkv")
        got = os_.find_orphans(on, {D + "plex.mkv"}, {D + "qbit.mkv"}, {D + "arr.mkv"})
        self.assertEqual(list(got), [D + "orphan.mkv"])


class Sanity(unittest.TestCase):
    def big(self, n=10):
        return disk(*[f"{i}.mkv" for i in range(n)])

    def test_healthy(self):
        on = self.big()
        self.assertIsNone(os_.sanity_problem(on, set(on), {"x"}, {"y"}))

    def test_wrong_plex_mapping_is_refused(self):
        on = self.big()
        self.assertIn("Plex", os_.sanity_problem(on, set(), {"x"}, {"y"}))
        self.assertIn("Plex", os_.sanity_problem(on, set(list(on)[:2]), {"x"}, {"y"}))

    def test_empty_indexes_are_refused(self):
        on = self.big()
        self.assertIn("qBittorrent", os_.sanity_problem(on, set(on), set(), {"y"}))
        self.assertIn("Radarr", os_.sanity_problem(on, set(on), {"x"}, set()))
        self.assertIn("no video", os_.sanity_problem({}, set(), {"x"}, {"y"}))


class ApplyFlow(unittest.TestCase):
    def run_main(self, on_disk, plex, qbit, arr, argv, raw=None, nlink=1):
        removed = []
        raw = raw or (lambda p: [p.replace("/mnt/user/", "/mnt/disk1/")])
        with mock.patch.object(os_, "load_qbit_known", return_value=qbit), mock.patch.object(os_, "load_plex_known", return_value=plex), \
                mock.patch.object(os_, "load_arr_known", return_value=arr), mock.patch.object(os_, "scan_disk", return_value=on_disk), \
                mock.patch.object(os_.safe_ops, "raw_copies", side_effect=raw), mock.patch.object(os_.safe_ops, "file_identity", return_value=(1, 1, nlink)), \
                mock.patch.object(os_.safe_ops, "unlink_raw", side_effect=lambda paths, apply, *a: removed.extend(paths) or []), \
                mock.patch.object(os_.safe_ops, "prune_empty_parents", return_value=[]), mock.patch.object(os_, "log"), mock.patch.object(sys, "argv", ["x"] + argv):
            rc = os_.main()
        return rc, removed

    def test_apply_deletes_only_true_orphans(self):
        on = disk(*[f"k{i}.mkv" for i in range(9)], "orphan.mkv")
        known = {p for p in on if "orphan" not in p}
        rc, removed = self.run_main(on, known, {"q"}, {"a"}, ["--apply"])
        self.assertEqual(removed, ["/mnt/disk1/Media/Movies/orphan.mkv"])

    def test_apply_refuses_when_plex_index_is_wrong(self):
        on = disk(*[f"f{i}.mkv" for i in range(10)])
        rc, removed = self.run_main(on, set(), {"q"}, {"a"}, ["--apply"])
        self.assertEqual((rc, removed), (1, []))

    def test_recent_hardlinked_excluded_and_multicopy_are_skipped(self):
        on = disk(*[f"k{i}.mkv" for i in range(8)])
        on.update({D + "recent.mkv": (10**9, time.time() - 3600), D + "linked.mkv": (10**9, OLD), D + "excl.mkv": (10**9, OLD), D + "two.mkv": (10**9, OLD)})
        known = {p for p in on if p.rsplit("/", 1)[1].startswith("k")}
        raw = lambda p: ["/mnt/disk1/x", "/mnt/disk2/x"] if p.endswith("two.mkv") else [p.replace("/mnt/user/", "/mnt/disk1/")]  # noqa: E731
        rc, removed = self.run_main(on, known, {"q"}, {"a"}, ["--apply", "--exclude", D + "excl.mkv"], raw=raw, nlink=2)
        self.assertEqual(removed, [])   # hardlinked (nlink 2) everywhere -> nothing deleted

    def test_recent_and_excluded_files_survive_even_when_unlinked_by_nobody(self):
        on = disk(*[f"k{i}.mkv" for i in range(8)])
        on.update({D + "recent.mkv": (10**9, time.time() - 3600), D + "excl.mkv": (10**9, OLD)})
        known = {p for p in on if p.rsplit("/", 1)[1].startswith("k")}
        rc, removed = self.run_main(on, known, {"q"}, {"a"}, ["--apply", "--exclude", D + "excl.mkv"])
        self.assertEqual(removed, [])

    def test_multi_disk_copy_is_never_deleted(self):
        on = disk(*[f"k{i}.mkv" for i in range(9)], "two.mkv")
        known = {p for p in on if "two" not in p}
        rc, removed = self.run_main(on, known, {"q"}, {"a"}, ["--apply"], raw=lambda p: ["/mnt/disk1/x", "/mnt/disk2/x"])
        self.assertEqual(removed, [])

    def test_dry_run_never_deletes(self):
        on = disk(*[f"k{i}.mkv" for i in range(9)], "orphan.mkv")
        known = {p for p in on if "orphan" not in p}
        rc, removed = self.run_main(on, known, {"q"}, {"a"}, [])
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
