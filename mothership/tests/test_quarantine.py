import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quarantine as q  # noqa: E402


def mk(mnt, disk, day, rel, data=b"x" * 10):
    p = os.path.join(mnt, disk, "Media", ".quarantine", day, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)
    return p


class Quarantine(unittest.TestCase):
    def setUp(self):
        self.mnt = tempfile.mkdtemp()
        self.p = mock.patch.object(q, "MNT", self.mnt); self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_purge_removes_only_old_dated_folders(self):
        old = mk(self.mnt, "disk1", "2026-10-01", "Movies/A/a.mkv"); new = mk(self.mnt, "disk1", "2026-10-20", "Movies/B/b.mkv")
        os.makedirs(f"{self.mnt}/disk1/Media/.quarantine/notes"); keepme = f"{self.mnt}/disk1/Media/.quarantine/notes/keep.txt"; open(keepme, "w").write("x")
        gone, freed = q.purge(7, False, today=datetime.date(2026, 10, 22))
        self.assertEqual(len(gone), 1); self.assertTrue(os.path.exists(old))                 # dry run changes nothing
        q.purge(7, True, today=datetime.date(2026, 10, 22))
        self.assertFalse(os.path.exists(old)); self.assertTrue(os.path.exists(new)); self.assertTrue(os.path.exists(keepme))   # only YYYY-MM-DD folders are ever touched

    def test_restore_moves_back_and_never_overwrites(self):
        p = mk(self.mnt, "disk2", "2026-10-21", "Movies/A/a.mkv")
        q.restore("2026-10-21/Movies/A/a.mkv", False); self.assertTrue(os.path.exists(p))
        q.restore("2026-10-21/Movies/A/a.mkv", True)
        self.assertTrue(os.path.exists(f"{self.mnt}/disk2/Media/Movies/A/a.mkv")); self.assertFalse(os.path.exists(p))
        mk(self.mnt, "disk2", "2026-10-22", "Movies/A/a.mkv")
        with self.assertRaises(SystemExit):
            q.restore("2026-10-22/Movies/A/a.mkv", True)                                      # destination exists now

    def test_restore_rejects_traversal_and_bad_days(self):
        for bad in ("../../etc/passwd", "2026-10-21/../../x", "notadate/Movies/a"):
            with self.assertRaises(SystemExit):
                q.restore(bad, True)

    def test_listing_reports_every_file_with_size(self):
        mk(self.mnt, "disk1", "2026-10-21", "tv/S/e.mkv", b"x" * 5)
        e = q.listing()
        self.assertEqual((e[0]["rel"], e[0]["size"]), ("tv/S/e.mkv", 5))


if __name__ == "__main__":
    unittest.main()
