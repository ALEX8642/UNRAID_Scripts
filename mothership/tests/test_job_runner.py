import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("requests", mock.MagicMock())
import job_runner as jr  # noqa: E402
import safe_ops  # noqa: E402

P1 = "/media/Movies/A (2020)/a.mkv"
RAW1 = "/mnt/disk1/Media/Movies/A (2020)/a.mkv"


class FakeDR:
    def __init__(self, torrent=None):
        self.APPLY = False
        self.QBIT_SESSION = object()
        self.QBIT_URL = "http://q"
        self.QBIT_TORRENTS = {"x": {}}
        self.QBIT_SKIPPED = 0
        self.torrent = torrent
        self.plex = 0

    def load_qbit_index(self):
        return {}

    def hardlink_problem(self, rec):
        return ""

    def torrent_for(self, rec):
        return self.torrent

    def plex_refresh_and_empty_trash(self):
        self.plex += 1


def job(**kw):
    j = dict(id="j1", type="delete_files", apply=False, plan_digest="", created=time.time(), app="radarr", arr_id=7, title="A", paths=[P1], remove_record=True)
    j.update(kw)
    return j


class Delete(unittest.TestCase):
    def setUp(self):
        self.requests = mock.MagicMock()
        self.requests.delete.return_value.ok = True
        self.requests.delete.return_value.status_code = 200
        self.requests.get.return_value.json.return_value = {"id": 7, "monitored": True}
        self.p = [mock.patch.object(jr, "requests", self.requests), mock.patch.object(jr, "raw_copies", side_effect=lambda p: [p.replace("/mnt/user/", "/mnt/disk1/")]),
                  mock.patch.object(jr.safe_ops, "file_identity", return_value=(1, 1, 1)), mock.patch.object(jr.os.path, "exists", return_value=True),
                  mock.patch.object(jr, "log"), mock.patch.object(jr, "PLEX_TOKEN", "t")]
        for x in self.p:
            x.start()
        self.executed = []
        self.q = mock.patch.object(jr.safe_ops, "execute", side_effect=lambda d, *a, **k: self.executed.append(d.mode) or (["quarantined: x"] if d.unlink_raw else ["qBit tagged"] if d.tag else ["qBit deleted"]))
        self.q.start()
        self.pr = mock.patch.object(jr.safe_ops, "prune_empty_parents", return_value=[])
        self.pr.start()
        self.tr = {P1}
        self.af = mock.patch.object(jr, "arr_file_paths", side_effect=lambda j: set(self.tr))
        self.af.start()
        self.isf = mock.patch.object(jr.os.path, "isfile", return_value=True)
        self.isf.start()
        self.isl = mock.patch.object(jr.os.path, "islink", return_value=False)
        self.isl.start()

    def tearDown(self):
        for x in self.p + [self.q, self.pr, self.af, self.isf, self.isl]:
            x.stop()

    def test_dry_run_plans_and_changes_nothing(self):
        dr = FakeDR()
        res = jr.run_delete(job(), dr)
        self.assertEqual(self.executed, [])
        self.assertTrue(res["digest"])
        self.assertTrue(res["removes_record"])
        self.requests.delete.assert_not_called()

    def test_apply_with_matching_digest_unlinks_and_removes_record(self):
        dr = FakeDR()
        d = jr.run_delete(job(), dr)["digest"]
        res = jr.run_delete(job(apply=True, plan_digest=d), FakeDR())
        self.assertEqual(self.executed, ["UNLINK"])
        self.requests.delete.assert_called_once()
        self.assertEqual(self.requests.delete.call_args.kwargs["params"]["deleteFiles"], "false")   # files were handled by safe_ops, never by the arr
        self.assertTrue(res["ok"])

    def test_apply_with_stale_digest_does_nothing(self):
        res = jr.run_delete(job(apply=True, plan_digest="deadbeef"), FakeDR())
        self.assertFalse(res["ok"]); self.assertEqual(self.executed, []); self.requests.delete.assert_not_called()

    def test_young_seed_is_tagged_and_record_is_kept_unmonitored(self):
        t = safe_ops.TorrentInfo("h", "n", seed_days=1, covers_exact_path=True)
        d = jr.run_delete(job(), FakeDR(t))["digest"]
        res = jr.run_delete(job(apply=True, plan_digest=d), FakeDR(t))
        self.assertEqual(self.executed, ["TAG_ONLY"])
        self.requests.delete.assert_not_called()
        self.assertFalse(self.requests.put.call_args.kwargs["json"]["monitored"])

    def test_refused_file_blocks_record_removal(self):
        j = job(paths=[P1, "/media/Movies/B (2021)/b.mkv"])
        self.tr = {P1, "/media/Movies/B (2021)/b.mkv"}
        raw = {"/mnt/user/Media/Movies/A (2020)/a.mkv": [RAW1], "/mnt/user/Media/Movies/B (2021)/b.mkv": ["/mnt/disk1/x", "/mnt/disk2/x"]}
        with mock.patch.object(jr, "raw_copies", side_effect=lambda p: raw[p]):
            d = jr.run_delete(j, FakeDR())["digest"]
            jr.run_delete(dict(j, apply=True, plan_digest=d), FakeDR())
        self.assertEqual(self.executed, ["UNLINK"])
        self.requests.delete.assert_not_called()

    def test_paths_outside_the_library_or_with_traversal_are_refused(self):
        for bad in ("/media/arr/movies/x.mkv", "/etc/passwd", "/media/Movies/../../etc/passwd", "/media/Movies/A/../../tv/x.mkv"):
            dec, lines = jr.plan_delete(job(paths=[bad]), FakeDR())
            self.assertEqual(dec[0][1].mode, "REFUSE", bad)

    def test_digest_binds_the_record_and_the_flags_not_just_the_files(self):
        d = jr.run_delete(job(), FakeDR())["digest"]
        for changed in (dict(arr_id=8), dict(remove_record=False), dict(app="sonarr")):
            res = jr.run_delete(job(apply=True, plan_digest=d, **changed), FakeDR())
            self.assertFalse(res["ok"], changed)
        self.assertEqual(self.executed, []); self.requests.delete.assert_not_called()

    def test_oversized_jobs_are_refused(self):
        with mock.patch.object(jr, "MAX_FILES_PER_JOB", 2):
            res = jr.run_delete(job(paths=[P1, P1 + "2", P1 + "3"]), FakeDR())
        self.assertFalse(res["ok"]); self.assertIn("safety limit", res["lines"][0])

    def test_empty_or_malformed_jobs_are_refused(self):
        for bad in (dict(paths=[]), dict(arr_id="7"), dict(app="plex")):
            self.assertFalse(jr.run_delete(job(**bad), FakeDR())["ok"], bad)

    def test_paths_the_app_does_not_track_for_that_record_are_refused(self):
        res = jr.run_delete(job(paths=["/media/Movies/Other (2001)/o.mkv"]), FakeDR())
        self.assertFalse(res["ok"]); self.assertEqual(self.executed, [])

    def test_a_subset_of_the_records_files_never_removes_the_record(self):
        self.tr = {P1, "/media/Movies/A (2020)/a.2160p.mkv"}
        d = jr.run_delete(job(), FakeDR())
        self.assertFalse(d["removes_record"])
        jr.run_delete(job(apply=True, plan_digest=d["digest"]), FakeDR())
        self.requests.delete.assert_not_called()

    def test_failed_execution_keeps_the_record(self):
        d = jr.run_delete(job(), FakeDR())["digest"]
        with mock.patch.object(jr.safe_ops, "execute", return_value=["missing: /x"]):
            jr.run_delete(job(apply=True, plan_digest=d), FakeDR())
        self.requests.delete.assert_not_called()

    def test_unexplained_hardlink_is_refused(self):
        dr = FakeDR(); dr.hardlink_problem = lambda rec: "3 names but only 2 found"
        dec, _ = jr.plan_delete(job(), dr)
        self.assertEqual(dec[0][1].mode, "REFUSE")

    def test_directories_and_symlinks_are_not_deletable(self):
        for kw in (dict(isfile=False), dict(islink=True)):
            with mock.patch.object(jr.os.path, "isfile", return_value=kw.get("isfile", True)), mock.patch.object(jr.os.path, "islink", return_value=kw.get("islink", False)):
                dec, _ = jr.plan_delete(job(), FakeDR())
            self.assertEqual(dec[0][1].mode, "REFUSE", kw)

    def test_file_removal_path_never_goes_through_user_share(self):
        d = jr.run_delete(job(), FakeDR())
        dec, _ = jr.plan_delete(job(), FakeDR())
        self.assertTrue(all(x.startswith("/mnt/disk") for x in dec[0][1].unlink_raw))


class Process(unittest.TestCase):
    def test_expired_job_is_not_run(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/pending")
        with mock.patch.object(jr, "JOBS", d):
            p = f"{d}/pending/j1.json"; json.dump(job(created=time.time() - 3600), open(p, "w"))
            with mock.patch.object(jr, "run_delete") as rd:
                jr.process(p, FakeDR())
            rd.assert_not_called()
            self.assertIn("expired", json.load(open(f"{d}/done/j1.json"))["result"]["lines"][0])
            self.assertFalse(os.path.exists(p))

    def test_failure_still_writes_a_result(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/pending")
        with mock.patch.object(jr, "JOBS", d), mock.patch.object(jr, "log"):
            p = f"{d}/pending/j2.json"; json.dump(job(id="j2"), open(p, "w"))
            with mock.patch.object(jr, "run_delete", side_effect=RuntimeError("boom")):
                jr.process(p, FakeDR())
            self.assertFalse(json.load(open(f"{d}/done/j2.json"))["result"]["ok"])


class RunnerRobustness(unittest.TestCase):
    def test_job_found_in_running_after_a_crash_is_never_replayed(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/running"); os.makedirs(f"{d}/pending")
        json.dump(job(apply=True), open(f"{d}/running/zzz.json", "w"))
        with mock.patch.object(jr, "JOBS", d), mock.patch.dict(sys.modules, {"dupe_review": FakeDR()}):
            jr.main()
        self.assertFalse(json.load(open(f"{d}/done/zzz.json"))["result"]["ok"]); self.assertEqual(os.listdir(f"{d}/running"), [])

    def test_delete_job_refused_when_qbit_index_is_incomplete(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/pending")
        p = f"{d}/pending/j9.json"; json.dump(job(), open(p, "w"))
        dr = FakeDR(); dr.QBIT_SKIPPED = 3
        with mock.patch.object(jr, "JOBS", d), mock.patch.object(jr, "run_delete") as rd:
            jr.process(p, dr)
        rd.assert_not_called(); self.assertIn("incomplete", json.load(open(f"{d}/done/j9.json"))["result"]["lines"][0])

    def test_malformed_job_does_not_block_the_next_one(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/pending")
        open(f"{d}/pending/aaa.json", "w").write("{not json")
        os.utime(f"{d}/pending/aaa.json", (time.time() - 3600, time.time() - 3600))    # old enough that it cannot be a write in progress
        json.dump(job(id="zzz", type="poster_reset", title="X"), open(f"{d}/pending/bbb.json", "w"))
        fake = FakeDR()
        with mock.patch.object(jr, "JOBS", d), mock.patch.object(jr, "log"), mock.patch.dict(sys.modules, {"dupe_review": fake}), \
                mock.patch.object(jr, "run_poster_reset", return_value={"ok": True, "lines": []}), mock.patch.object(fake, "load_qbit_index", create=True):
            jr.main()
        self.assertFalse(json.load(open(f"{d}/done/aaa.json"))["result"]["ok"])
        self.assertTrue(json.load(open(f"{d}/done/bbb.json"))["result"]["ok"])

    def test_job_id_inside_the_file_cannot_redirect_the_result(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/pending")
        p = f"{d}/pending/real.json"; json.dump(job(id="../../etc/evil", created=time.time() - 3600), open(p, "w"))
        with mock.patch.object(jr, "JOBS", d):
            jr.process(p, FakeDR())
        self.assertTrue(os.path.exists(f"{d}/done/real.json"))
        self.assertFalse(os.path.exists(f"{d}/../etc/evil.json"))


class Poster(unittest.TestCase):
    def test_reset_removes_backup_and_label_only_on_apply(self):
        k = tempfile.mkdtemp(); os.makedirs(f"{k}/overlays/Movies Original Posters"); b = f"{k}/overlays/Movies Original Posters/55.jpg"; open(b, "w").close()
        rq = mock.MagicMock(); rq.get.return_value.json.return_value = {"MediaContainer": {"Metadata": [{"title": "Rambo", "year": 2019, "ratingKey": "55"}]}}
        with mock.patch.object(jr, "requests", rq), mock.patch.object(jr, "KOMETA_DIR", k), mock.patch.object(jr, "log"):
            jr.run_poster_reset({"title": "Rambo", "apply": False})
            self.assertTrue(os.path.exists(b)); rq.put.assert_not_called()
            res = jr.run_poster_reset({"title": "Rambo", "apply": True})
        self.assertFalse(os.path.exists(b)); rq.put.assert_called_once(); self.assertTrue(res["ok"])

    def test_unknown_title_does_nothing(self):
        rq = mock.MagicMock(); rq.get.return_value.json.return_value = {"MediaContainer": {}}
        with mock.patch.object(jr, "requests", rq):
            self.assertFalse(jr.run_poster_reset({"title": "Nope", "apply": True})["ok"])


if __name__ == "__main__":
    unittest.main()
