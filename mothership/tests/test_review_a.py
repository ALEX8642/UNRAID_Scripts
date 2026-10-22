import errno, json, os, sys, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for m in ("requests", "rich", "rich.console", "rich.table", "rich.progress", "rich.panel", "rich.prompt", "rich.box"):
    sys.modules.setdefault(m, mock.MagicMock())
import dupe_review as dr  # noqa: E402
import safe_ops as so  # noqa: E402
from test_dupe_review import rec  # noqa: E402


class FakeSession:
    def __init__(self, ok=True): self.calls = []; self.ok = ok
    def post(self, url, data=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], dict(data or {})))
        return SimpleNamespace(ok=self.ok, status_code=200 if self.ok else 500)


def tree(files=("a.mkv",)):
    d = tempfile.mkdtemp(); os.makedirs(f"{d}/disk1/Media/Movies/A")
    for f in files:
        open(f"{d}/disk1/Media/Movies/A/{f}", "w").write("x")
    return d


class F1_FailedRemovalReportedAsDeleted(unittest.TestCase):
    def _delete_one(self, rename_fails=True, qbit_ok=True, torrent=None):
        d = tree(); raw = f"{d}/disk1/Media/Movies/A/a.mkv"
        r = rec(); r.raw = [raw]; r.nlink = 1
        keep = rec("/mnt/user/Media/Movies/A/k.mkv"); keep.raw = ["/nonexistent"]
        sess = FakeSession(qbit_ok)
        patches = [mock.patch.object(dr, "APPLY", True), mock.patch.object(dr, "QBIT_SESSION", sess), mock.patch.object(dr, "torrent_for", return_value=torrent),
                   mock.patch.object(dr, "veto", side_effect=lambda dec, *a, **k: dec), mock.patch.object(dr, "hardlink_problem", return_value=""),
                   mock.patch.object(dr, "Prompt", mock.MagicMock(ask=lambda *a, **k: "y")), mock.patch.object(dr, "LOGFILE", ""), mock.patch.object(dr, "log")]
        if rename_fails:
            patches.append(mock.patch.object(so.os, "rename", side_effect=OSError(errno.EACCES, "denied")))
        for p in patches: p.start()
        try:
            out = dr.delete_one(r, [keep], False, "s")
        finally:
            mock.patch.stopall()
        return out, os.path.exists(raw), sess

    def test_aborted_quarantine_is_not_reported_as_deleted(self):
        out, still_there, _ = self._delete_one()
        self.assertTrue(still_there)
        self.assertNotEqual(out, "deleted")      # main() then marks the title reviewed / runs reconcile_arr / counts freed bytes

    def test_failed_qbit_delete_is_not_reported_as_deleted(self):
        t = so.TorrentInfo("h", "n", 200)
        out, _, sess = self._delete_one(rename_fails=False, qbit_ok=False, torrent=t)
        self.assertEqual([c[0] for c in sess.calls], ["delete"])
        self.assertNotEqual(out, "deleted")


class F2_ExecutePlanReconcilesAfterAbort(unittest.TestCase):
    def test_reconcile_not_called_when_nothing_was_removed(self):
        a = dict(title="A", kind="movie", loser="/mnt/user/Media/Movies/A/lose.mkv", keep=["/mnt/user/Media/Movies/A/keep.mkv"], mode="UNLINK",
                 torrent="", arr="Radarr", arr_id=7, size=1)
        p = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump([a], p); p.close()
        raw = {a["loser"]: ["/mnt/disk1/Media/Movies/A/lose.mkv"], a["keep"][0]: ["/mnt/disk1/Media/Movies/A/keep.mkv"]}
        rc = mock.MagicMock(); mock.patch.object(dr, "LOGFILE", "").start(); mock.patch.object(dr, "log").start(); self.addCleanup(mock.patch.stopall)
        with mock.patch.object(dr, "APPLY", True), mock.patch.object(dr, "load_qbit_index"), mock.patch.object(dr, "raw_copies", side_effect=lambda x: raw.get(x, [])), \
                mock.patch.object(dr, "torrent_for", return_value=None), mock.patch.object(dr, "plex_refresh_and_empty_trash"), mock.patch.object(dr, "reconcile_arr", rc), \
                mock.patch.object(dr.safe_ops, "unlink_raw", return_value=[("/mnt/disk1/Media/Movies/A/lose.mkv", "failed: EACCES")]), \
                mock.patch.object(dr.safe_ops, "file_identity", return_value=(1, 1, 1)), mock.patch.object(dr.os.path, "exists", return_value=True), mock.patch.object(dr.os.path, "getsize", return_value=1), \
                mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_TORRENTS", {"x": {}}), mock.patch.object(dr, "QBIT_SKIPPED", 0), \
                mock.patch.object(dr.safe_ops, "keeper_problem", return_value=""), mock.patch.object(dr, "hardlink_problem", return_value=""):
            dr.execute_plan(p.name)
        rc.assert_not_called()      # Radarr was just repointed at the keeper although the loser is still on disk


class F3_MultiFileSeedDeletedWithFiles(unittest.TestCase):
    def test_deletefiles_true_never_used_on_a_multifile_torrent_whose_other_files_were_not_quarantined(self):
        t = so.TorrentInfo("h", "Movie.CD1-CD2", seed_days=200, n_files=2, covers_exact_path=False)
        dec = so.classify_removal(["/mnt/disk1/Media/Movies/A/a.mkv"], t)
        self.assertEqual(dec.mode, "LIBRARY_NAME_THEN_DELETE")
        # Only ONE name (the library hardlink) gets quarantined; the torrent's 2nd file lives only in arr/ and
        # qBittorrent deleteFiles=true would erase it with no copy anywhere.
        self.assertFalse(dec.delete_files and t.n_files > 1)


class F4_NewlineInNames(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.lib = f"{d}/disk1/Media/Movies/A/a.mkv"
        os.makedirs(os.path.dirname(self.lib)); os.makedirs(f"{d}/disk1/Media/arr/movies/X")
        open(self.lib, "w").write("x")
        self.seed = f"{d}/disk1/Media/arr/movies/X/seed\nfoo.mkv"; os.link(self.lib, self.seed)

    def test_other_names_returns_one_entry_per_file(self):
        self.assertEqual(so.other_names(self.lib), [self.seed])

    def test_hardlink_problem_not_fooled_by_newline_name(self):
        extra = os.path.join(os.path.dirname(self.lib), "hidden-third-name.mkv")      # a name outside arr/ (e.g. a seed elsewhere)
        os.link(self.lib, extra)
        r = SimpleNamespace(path="/mnt/user/Media/Movies/A/a.mkv", raw=[self.lib], nlink=os.stat(self.lib).st_nlink)
        # nlink=3: lib, arr seed (newline name), hidden third.  Only 1 real name found under arr/, so this must be refused.
        self.assertIn("another name", dr.hardlink_problem(r))

    def test_torrent_for_does_not_crash_on_newline_name(self):
        r = SimpleNamespace(path="/mnt/user/Media/Movies/A/a.mkv", raw=[self.lib], nlink=2)
        with mock.patch.object(dr, "QBIT_SESSION", object()), mock.patch.object(dr, "QBIT_INDEX", {}), mock.patch.object(dr, "QBIT_TORRENTS", {}):
            dr.torrent_for(r)       # IndexError on the 2nd half of the split name


class F5_NFilesTrustsErrorBodies(unittest.TestCase):
    def test_non_list_or_error_response_is_unknown(self):
        sess = mock.MagicMock(); resp = sess.get.return_value
        resp.ok = False; resp.status_code = 500; resp.json.return_value = {"error": "x"}
        with mock.patch.object(dr, "QBIT_SESSION", sess):
            self.assertEqual(dr.qbit_n_files("h"), 999)       # returns 1 -> a 40-file pack looks like a single file


class F6_FuseGuardPrefix(unittest.TestCase):
    def test_user0_fuse_view_is_refused(self):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/user0/Media/Movies"); f = f"{d}/user0/Media/Movies/a.mkv"; open(f, "w").write("x")
        with mock.patch.object(so, "USER_PREFIX", f"{d}/user/"):
            out = so.unlink_raw([f], True)
        self.assertTrue(out[0][1].startswith("refused"), out)


class F7_NonLibraryRootPrefix(unittest.TestCase):
    def test_prefix_match_needs_a_path_boundary(self):
        s = mock.MagicMock(); s.post.return_value.status_code = 200; s.post.return_value.text = "Ok."
        s.get.return_value.json.return_value = [{"hash": "h", "content_path": "/runway-movies/X/x.mkv", "save_path": "/runway-movies/X"}]
        with mock.patch.object(dr.requests, "Session", return_value=s), mock.patch.object(dr, "QBIT_INDEX", {}):
            dr.load_qbit_index()
        self.assertEqual(dr.QBIT_SKIPPED, 1)


if __name__ == "__main__":
    unittest.main()
