import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("requests", mock.MagicMock())
import seed_cleanup as sc  # noqa: E402

NOW = time.time()


def torrent(h, name, **kw):
    t = dict(hash=h, name=name, state="missingFiles", content_path=f"/movies/{name}", added_on=NOW - 40 * 86400,
             completion_on=NOW - 39 * 86400, ratio=0.2)
    t.update(kw)
    return t


class FakeSession:
    def __init__(self, torrents, files):
        self.torrents, self.files, self.deleted = torrents, files, []

    def post(self, url, data=None, **kw):
        r = mock.MagicMock()
        if url.endswith("/auth/login"):
            r.text = "Ok."
        elif url.endswith("/torrents/delete"):
            self.deleted.append(data["hashes"])
        return r

    def get(self, url, params=None, **kw):
        r = mock.MagicMock()
        r.json.return_value = self.torrents if url.endswith("/torrents/info") else self.files[params["hash"]]
        return r


def run(torrents, deleted_names, apply=True, on_array=False, state=None):
    files = {t["hash"]: [{"name": t["name"] + "/" + t["name"] + ".mkv", "size": 5}] for t in torrents}
    fs = FakeSession(torrents, files)
    st = {} if state is None else state
    out = io.StringIO()
    with mock.patch.object(sc.requests, "Session", return_value=fs), mock.patch.object(sc, "exists_on_array", return_value=on_array), \
            mock.patch.object(sc, "upgrade_deleted_names", return_value=deleted_names), mock.patch("os.path.isdir", return_value=True), \
            mock.patch("builtins.open", mock.mock_open(read_data=__import__("json").dumps(st))), mock.patch("os.makedirs"), \
            mock.patch.object(sys, "argv", ["x"] + (["--apply"] if apply else [])), redirect_stdout(out):
        sc.main()
    return fs.deleted, out.getvalue()


OLD = {"a": NOW - 30 * 3600}


class SeedCleanup(unittest.TestCase):
    def test_removes_after_second_sighting(self):
        t = torrent("a", "Movie.A")
        d, _ = run([t], {"Movie.A.mkv"}, state=dict(OLD))
        self.assertEqual(d, ["a"])

    def test_first_sighting_waits(self):
        d, out = run([torrent("a", "Movie.A")], {"Movie.A.mkv"}, state={})
        self.assertEqual(d, [])
        self.assertIn("second sighting", out)

    def test_content_still_on_array_is_kept(self):
        d, _ = run([torrent("a", "Movie.A")], {"Movie.A.mkv"}, on_array=True, state=dict(OLD))
        self.assertEqual(d, [])

    def test_no_upgrade_record_is_kept(self):
        d, out = run([torrent("a", "Movie.A")], {"Other.mkv"}, state=dict(OLD))
        self.assertEqual(d, [])
        self.assertIn("no Radarr/Sonarr upgrade-deletion record", out)

    def test_seeding_rule_not_met_waits(self):
        t = torrent("a", "Movie.A", completion_on=NOW - 86400, added_on=NOW - 86400)
        d, out = run([t], {"Movie.A.mkv"}, state=dict(OLD))
        self.assertEqual(d, [])
        self.assertIn("not met", out)

    def test_ratio_satisfies_rule(self):
        t = torrent("a", "Movie.A", completion_on=NOW - 86400, added_on=NOW - 86400, ratio=1.3)
        d, _ = run([t], {"Movie.A.mkv"}, state=dict(OLD))
        self.assertEqual(d, ["a"])

    def test_circuit_breaker(self):
        ts = [torrent(str(i), f"Movie.{i}") for i in range(7)]
        st = {str(i): NOW - 30 * 3600 for i in range(7)}
        d, out = run(ts, {f"Movie.{i}.mkv" for i in range(7)}, state=st)
        self.assertEqual(d, [])
        self.assertIn("CIRCUIT BREAKER", out)

    def test_dry_run_removes_nothing(self):
        d, out = run([torrent("a", "Movie.A")], {"Movie.A.mkv"}, apply=False, state=dict(OLD))
        self.assertEqual(d, [])
        self.assertIn("WOULD REMOVE", out)


if __name__ == "__main__":
    unittest.main()


class ContentPresent(unittest.TestCase):
    def test_directory_exists_but_own_files_gone(self):
        # the folder exists (new library files live in it) but none of THIS torrent's files do -> treated as absent
        t = torrent("a", "Show.S01", content_path="/tv/Show.S01", save_path="/tv")
        fs = FakeSession([t], {"a": [{"name": "Show.S01/old.mkv", "size": 5}]})
        with mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p == "/tv/Show.S01"):
            self.assertFalse(sc.content_present(fs, t))
        with mock.patch.object(sc, "exists_on_array", side_effect=lambda p: p == "/tv/Show.S01/old.mkv"):
            self.assertTrue(sc.content_present(fs, t))
