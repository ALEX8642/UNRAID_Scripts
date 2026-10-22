"""Hostile-review round C: cross-disk-dedupe.sh. Tests encode the CORRECT behaviour."""
import os
import stat
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_cross_disk_dedupe import Run, two_copies, write, REL  # noqa: E402

MB = 1024 * 1024


class DedupeC(unittest.TestCase):
    def setUp(self):
        self.r = Run()

    def tearDown(self):
        self.r.cleanup()

    def test_C50_middle_corruption_is_not_detected_although_docs_promise_sha256(self):
        """Header says 'Full SHA-256 checksum of both copies', README says 'SHA-256 + ctime'; the code hashes only the first/last 4 MB."""
        a = b"A" * (6 * MB) + b"X" * (2 * MB) + b"A" * (6 * MB)      # exposed copy
        b = b"A" * (6 * MB) + b"Y" * (2 * MB) + b"A" * (6 * MB)      # different content in the middle, same size
        two_copies(self.r, data_a=a, data_b=b, expose="disk1")
        out = self.r.go("--apply")
        self.assertTrue(os.path.exists(self.r.p("disk2", REL)), f"copy with DIFFERENT content was deleted as a 'duplicate': {out[-400:]}")

    def test_C51_apply_is_an_irreversible_rm_not_a_quarantine(self):
        two_copies(self.r, expose="disk1")
        self.r.go("--apply")
        q = self.r.p("disk2", "Media", ".quarantine")
        found = [os.path.join(rt, f) for rt, _d, fs in os.walk(q) for f in fs] if os.path.isdir(q) else []
        self.assertTrue(found, "every other removal path quarantines (7-day undo); cross-disk-dedupe --apply `rm -f`s the file for good")

    def test_C52_failed_rm_is_logged_as_deleted(self):
        two_copies(self.r, expose="disk1")
        d = os.path.dirname(self.r.p("disk2", REL))
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)        # rm -f cannot unlink here
        try:
            out = self.r.go("--apply")
        finally:
            os.chmod(d, stat.S_IRWXU)
        self.assertTrue(os.path.exists(self.r.p("disk2", REL)))
        self.assertNotIn("DELETED (redundant", out, "rm failed (file still there) but the log says DELETED and the summary counts it as freed")

    def test_C53_awkward_names_never_delete_anything_wrong(self):
        """probe: quote, $, tab, newline in names (should resolve or skip, never delete the exposed copy)"""
        for name in ["it's \"q\" $HOME `x`.mkv", "tab\there.mkv", "new\nline.mkv"]:
            r = Run()
            try:
                rel = f"Media/Movies/Z (2020)/{name}"
                write(r.p("disk2", rel)); time.sleep(1.1); write(r.p("disk1", rel))
                os.makedirs(os.path.dirname(r.p("user", rel)), exist_ok=True); os.link(r.p("disk1", rel), r.p("user", rel))
                r.go("--apply")
                self.assertTrue(os.path.exists(r.p("disk1", rel)), name)
                self.assertTrue(os.path.exists(r.p("user", rel)), name)
            finally:
                r.cleanup()

    def test_C54_three_disks_skipped(self):
        two_copies(self.r, expose="disk1")
        write(self.r.p("disk3", REL))
        self.r.go("--apply")
        for d in ("disk1", "disk2", "disk3"):
            self.assertTrue(os.path.exists(self.r.p(d, REL)), d)


if __name__ == "__main__":
    unittest.main()
