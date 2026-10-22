"""Runs the real cross-disk-dedupe.sh against a scratch /mnt tree. Needs bash, find, stat, sha256sum, curl."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "cross-disk-dedupe.sh")
REL = "Media/Movies/A (2020)/a.mkv"


def write(path, data=b"x" * 5000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class Run:
    def __init__(self):
        self.d = tempfile.mkdtemp()
        self.mnt = os.path.join(self.d, "mnt")
        os.makedirs(self.mnt)

    def go(self, *args):
        env = dict(os.environ, MNT=self.mnt, WORKDIR=os.path.join(self.d, "work"), LOGFILE=os.path.join(self.d, "log.txt"), LOCKFILE=os.path.join(self.d, "lock"),
                   QBIT_PASS="x", QBIT_URL="http://127.0.0.1:1")
        p = subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=120)
        return p.stdout + p.stderr

    def p(self, *parts):
        return os.path.join(self.mnt, *parts)

    def cleanup(self):
        shutil.rmtree(self.d, ignore_errors=True)


def two_copies(r, data_a=b"x" * 5000, data_b=b"x" * 5000, expose="disk1"):
    """disk2 copy is written first, then (>1 s later, so ctimes differ) disk1; /mnt/user is a hardlink of the exposed copy."""
    first, second = ("disk2", "disk1") if expose == "disk1" else ("disk1", "disk2")
    write(r.p(first, REL), data_b if first == "disk2" else data_a)
    time.sleep(1.1)
    write(r.p(second, REL), data_a if second == "disk1" else data_b)
    os.makedirs(os.path.dirname(r.p("user", REL)), exist_ok=True)
    os.link(r.p(expose, REL), r.p("user", REL))


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.r = Run()

    def tearDown(self):
        self.r.cleanup()

    def test_dry_run_deletes_nothing(self):
        two_copies(self.r)
        out = self.r.go()
        self.assertIn("WOULD QUARANTINE", out)
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)) and os.path.exists(self.r.p("disk2", REL)))

    def test_apply_deletes_only_the_copy_user_share_does_not_expose(self):
        two_copies(self.r, expose="disk1")
        out = self.r.go("--apply")
        self.assertIn("QUARANTINED", out)
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)))
        self.assertFalse(os.path.exists(self.r.p("disk2", REL)))
        self.assertTrue(os.path.exists(self.r.p("user", REL)))

    def test_exposed_copy_is_judged_by_ctime_not_by_disk_order(self):
        two_copies(self.r, expose="disk2")
        self.r.go("--apply")
        self.assertTrue(os.path.exists(self.r.p("disk2", REL)))
        self.assertFalse(os.path.exists(self.r.p("disk1", REL)))

    def test_different_content_same_size_is_skipped(self):
        two_copies(self.r, data_a=b"a" * 5000, data_b=b"b" * 5000)
        out = self.r.go("--apply")
        self.assertIn("SKIP", out)
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)) and os.path.exists(self.r.p("disk2", REL)))

    def test_size_mismatch_is_skipped(self):
        two_copies(self.r, data_a=b"a" * 5000, data_b=b"a" * 4000)
        self.r.go("--apply")
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)) and os.path.exists(self.r.p("disk2", REL)))

    def test_three_copies_are_skipped(self):
        two_copies(self.r)
        write(self.r.p("disk3", REL))
        out = self.r.go("--apply")
        self.assertIn("not 2", out)
        for d in ("disk1", "disk2", "disk3"):
            self.assertTrue(os.path.exists(self.r.p(d, REL)))

    def test_missing_user_share_path_is_skipped(self):
        write(self.r.p("disk1", REL)); write(self.r.p("disk2", REL))
        self.r.go("--apply")
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)) and os.path.exists(self.r.p("disk2", REL)))

    def test_ambiguous_ctime_is_skipped(self):
        # both copies created within the same second and /mnt/user a separate third file: nothing identifies the exposed one
        write(self.r.p("disk1", REL)); write(self.r.p("disk2", REL)); write(self.r.p("user", REL))
        out = self.r.go("--apply")
        self.assertTrue(os.path.exists(self.r.p("disk1", REL)) and os.path.exists(self.r.p("disk2", REL)))

    def test_sidecars_are_reported_never_deleted(self):
        rel = "Media/Movies/A (2020)/a.nfo"
        write(self.r.p("disk1", rel)); write(self.r.p("disk2", rel)); write(self.r.p("user", rel))
        out = self.r.go("--apply")
        self.assertIn("REPORT", out)
        self.assertTrue(os.path.exists(self.r.p("disk1", rel)) and os.path.exists(self.r.p("disk2", rel)))

    def test_refuses_without_disks(self):
        out = self.r.go("--apply")
        self.assertIn("FATAL", out)


if __name__ == "__main__":
    unittest.main()
