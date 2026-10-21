import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("requests", mock.MagicMock())
import requests  # noqa: E402  (the stub above if the real package is absent)
import hnr_guard as hg  # noqa: E402

NOW = time.time()


class RequestException(Exception):
    pass


hg.requests.RequestException = RequestException


def tor(h, name, age_days, ratio=0.1, save="/movies"):
    return dict(hash=h, name=name, save_path=save, completion_on=NOW - age_days * 86400, added_on=NOW - age_days * 86400, ratio=ratio)


class FakeQ:
    def __init__(self, torrents, files):
        self.t, self.f = torrents, files

    def get(self, url, params=None, timeout=None):
        r = mock.MagicMock()
        r.json.return_value = self.t if url.endswith("/torrents/info") else [{"name": n} for n in self.f[params["hash"]]]
        return r


def run(torrents, files, movies, state=None, apply=True, put_error=None):
    tmp = tempfile.mkdtemp()
    sp = os.path.join(tmp, "state.json")
    json.dump(state or {"radarr": {}, "sonarr": {}}, open(sp, "w"))
    puts = []

    def arr_get(app, path):
        if app == "radarr" and path == "movie":
            return movies
        if app == "radarr":
            return next(m for m in movies if f"movie/{m['id']}" == path)
        if app == "sonarr" and path == "series":
            return []
        raise AssertionError(path)

    def arr_put(app, path, body):
        if put_error:
            raise put_error
        puts.append((app, path, body.get("monitored")))

    out = io.StringIO()
    with mock.patch.object(hg, "qbit_session", return_value=FakeQ(torrents, files)), mock.patch.object(hg, "arr_get", side_effect=arr_get), \
            mock.patch.object(hg, "arr_put", side_effect=arr_put), mock.patch.object(hg, "STATE", sp), mock.patch.object(hg, "EXTRA", os.path.join(tmp, "none.txt")), \
            mock.patch.object(sys, "argv", ["x"] + (["--apply"] if apply else [])), redirect_stdout(out):
        try:
            hg.main()
        except SystemExit as e:
            return puts, json.load(open(sp)), str(e)
    return puts, json.load(open(sp)), out.getvalue()


def movie(i, fname, monitored=True):
    return dict(id=i, title=f"M{i}", monitored=monitored, movieFile=dict(relativePath=fname))


class HnrGuardTests(unittest.TestCase):
    def test_young_library_seed_is_held(self):
        puts, st, _ = run([tor("a", "A", 1)], {"a": ["A/a.mkv"]}, [movie(1, "a.mkv")])
        self.assertEqual(puts, [("radarr", "movie/1", False)])
        self.assertIn("1", st["radarr"])

    def test_dry_run_changes_nothing(self):
        puts, st, out = run([tor("a", "A", 1)], {"a": ["A/a.mkv"]}, [movie(1, "a.mkv")], apply=False)
        self.assertEqual(puts, []); self.assertEqual(st["radarr"], {}); self.assertIn("DRY", out)

    def test_seed_in_arr_folder_is_never_held(self):
        puts, _, _ = run([tor("a", "A", 1, save="/arr/movies")], {"a": ["a.mkv"]}, [movie(1, "a.mkv")])
        self.assertEqual(puts, [])

    def test_satisfied_by_age_or_ratio_is_not_held(self):
        for t in (tor("a", "A", 6), tor("a", "A", 1, ratio=1.2)):
            puts, _, _ = run([t], {"a": ["a.mkv"]}, [movie(1, "a.mkv")])
            self.assertEqual(puts, [])

    def test_release_when_rule_met(self):
        state = {"radarr": {"1": {"title": "M1", "hashes": ["a"]}}, "sonarr": {}}
        puts, st, _ = run([tor("a", "A", 6), tor("b", "B", 1)], {"a": ["a.mkv"], "b": ["b.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=state)
        self.assertEqual(puts, [("radarr", "movie/1", True)]); self.assertEqual(st["radarr"], {})

    def test_user_unmonitored_items_are_not_touched(self):
        puts, st, _ = run([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv", monitored=False)])
        self.assertEqual(puts, []); self.assertEqual(st["radarr"], {})

    def test_empty_torrent_list_aborts_instead_of_releasing_everything(self):
        state = {"radarr": {"1": {"title": "M1", "hashes": ["a"]}}, "sonarr": {}}
        puts, st, msg = run([], {}, [movie(1, "a.mkv", monitored=False)], state=state)
        self.assertEqual(puts, []); self.assertIn("refusing", msg); self.assertIn("1", st["radarr"])

    def test_failed_update_keeps_state_and_does_not_crash(self):
        puts, st, out = run([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")], put_error=RequestException("boom"))
        self.assertEqual(st["radarr"], {}); self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
