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
import hnr_guard as hg  # noqa: E402


class RequestException(Exception):
    pass


hg.requests.RequestException = RequestException

NOW = time.time()


def tor(h, name, age_days, ratio=0.1, save="/movies", state="stalledUP", seeding_time=0):
    return dict(hash=h, name=name, save_path=save, completion_on=NOW - age_days * 86400, added_on=NOW - age_days * 86400, ratio=ratio, state=state, seeding_time=seeding_time)


class FakeQ:
    def __init__(self, torrents, files):
        self.t, self.f = torrents, files

    def get(self, url, params=None, timeout=None):
        r = mock.MagicMock()
        r.json.return_value = self.t if url.endswith("/torrents/info") else [{"name": n} for n in self.f[params["hash"]]]
        return r


class Env:
    """Runs hg.main() against fakes. radarr: list of movies; sonarr_series: list or an exception."""

    def __init__(self, torrents, files, movies, state=None, apply=True, put_error=None, sonarr_series=None, raw_state=None):
        self.tmp = tempfile.mkdtemp()
        self.sp = os.path.join(self.tmp, "state.json")
        if raw_state is not None:
            open(self.sp, "w").write(raw_state)
        elif state is not None:
            json.dump(state, open(self.sp, "w"))
        self.puts, self.torrents, self.files, self.movies = [], torrents, files, movies
        self.apply, self.put_error, self.sonarr_series = apply, put_error, [] if sonarr_series is None else sonarr_series

    def arr_get(self, app, path):
        if app == "radarr" and path == "movie":
            return self.movies
        if app == "radarr":
            return next(m for m in self.movies if f"movie/{m['id']}" == path)
        if app == "sonarr" and path == "series":
            if isinstance(self.sonarr_series, Exception):
                raise self.sonarr_series
            return self.sonarr_series
        raise AssertionError(path)

    def arr_put(self, app, path, body):
        if self.put_error:
            raise self.put_error
        self.puts.append((app, path, body.get("monitored")))

    def run(self):
        out = io.StringIO()
        with mock.patch.object(hg, "qbit_session", return_value=FakeQ(self.torrents, self.files)), mock.patch.object(hg, "arr_get", side_effect=self.arr_get), \
                mock.patch.object(hg, "arr_put", side_effect=self.arr_put), mock.patch.object(hg, "STATE", self.sp), mock.patch.object(hg, "LOCK", os.path.join(self.tmp, "lock")), \
                mock.patch.object(hg, "EXTRA", os.path.join(self.tmp, "none.txt")), mock.patch.object(sys, "argv", ["x"] + (["--apply"] if self.apply else [])), redirect_stdout(out):
            try:
                rc = hg.main()
            except SystemExit as e:
                rc = str(e)
        st = json.load(open(self.sp)) if os.path.exists(self.sp) else None
        return rc, st, out.getvalue()


def movie(i, fname, monitored=True):
    return dict(id=i, title=f"M{i}", monitored=monitored, movieFile=dict(relativePath=fname))


def hold(h="a", absent=0):
    return {"radarr": {"1": {"title": "M1", "hashes": [h], "absent": absent}}, "sonarr": {}, "_n": 0}


class HnrGuardTests(unittest.TestCase):
    def test_young_library_seed_is_held_and_the_hold_names_its_torrent(self):
        e = Env([tor("a", "A", 1)], {"a": ["A/a.mkv"]}, [movie(1, "a.mkv")])
        rc, st, _ = e.run()
        self.assertEqual(e.puts, [("radarr", "movie/1", False)]); self.assertEqual(st["radarr"]["1"]["hashes"], ["a"])

    def test_dry_run_changes_nothing(self):
        e = Env([tor("a", "A", 1)], {"a": ["A/a.mkv"]}, [movie(1, "a.mkv")], apply=False)
        rc, st, out = e.run()
        self.assertEqual(e.puts, []); self.assertIsNone(st); self.assertIn("DRY", out)

    def test_seed_in_arr_folder_is_never_held_but_arrival_is_not_arr(self):
        e = Env([tor("a", "A", 1, save="/arr/movies")], {"a": ["a.mkv"]}, [movie(1, "a.mkv")]); e.run()
        self.assertEqual(e.puts, [])
        e = Env([tor("a", "A", 1, save="/arrival")], {"a": ["a.mkv"]}, [movie(1, "a.mkv")]); e.run()
        self.assertEqual(len(e.puts), 1)

    def test_satisfied_torrents_are_not_held(self):
        for t in (tor("a", "A", 6), tor("a", "A", 1, ratio=1.2), tor("a", "A", 1, seeding_time=130 * 3600)):
            e = Env([t], {"a": ["a.mkv"]}, [movie(1, "a.mkv")]); e.run()
            self.assertEqual(e.puts, [], t)

    def test_paused_or_stopped_torrent_with_wall_clock_age_is_still_held(self):
        for st in ("pausedUP", "stoppedUP", "missingFiles", "error"):
            e = Env([tor("a", "A", 6, state=st)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")]); e.run()
            self.assertEqual(len(e.puts), 1, st)       # it never really seeded 120 h: keep the item protected

    def test_never_completed_torrent_is_held(self):
        t = tor("a", "A", 1); t["completion_on"] = -1
        e = Env([t], {"a": ["a.mkv"]}, [movie(1, "a.mkv")]); e.run()
        self.assertEqual(len(e.puts), 1)

    def test_release_when_its_own_torrent_met_the_rule(self):
        e = Env([tor("a", "A", 6), tor("b", "B", 1)], {"a": ["a.mkv"], "b": ["b.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=hold("a"))
        rc, st, _ = e.run()
        self.assertEqual(e.puts, [("radarr", "movie/1", True)]); self.assertEqual(st["radarr"], {})

    def test_no_release_while_its_torrent_is_still_young_or_paused(self):
        for t in (tor("a", "A", 1), tor("a", "A", 6, state="pausedUP")):
            e = Env([t, tor("z", "Z", 9)], {"a": ["a.mkv"], "z": ["z.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=hold("a")); e.run()
            self.assertEqual(e.puts, [], t)

    def test_unrelated_torrent_list_does_not_release_a_hold(self):
        e = Env([tor("b", "B", 9)], {"b": ["b.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=hold("a"))
        rc, st, _ = e.run()
        self.assertEqual(e.puts, []); self.assertEqual(st["radarr"]["1"]["absent"], {"a": 1})     # one absence is only noted

    def test_torrent_absent_on_two_consecutive_runs_releases(self):
        e = Env([tor("b", "B", 9)], {"b": ["b.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=hold("a", absent=1))
        e.run()
        self.assertEqual(e.puts, [("radarr", "movie/1", True)])

    def test_hold_without_recorded_torrents_is_never_released_blindly(self):
        st = {"radarr": {"1": {"title": "M1", "hashes": []}}, "sonarr": {}, "_n": 0}
        e = Env([tor("b", "B", 9)], {"b": ["b.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=st); e.run()
        self.assertEqual(e.puts, [])

    def test_user_unmonitored_items_are_not_touched(self):
        e = Env([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv", monitored=False)]); rc, st, _ = e.run()
        self.assertEqual(e.puts, []); self.assertEqual(st["radarr"], {})

    def test_empty_or_tiny_torrent_list_aborts(self):
        e = Env([], {}, [movie(1, "a.mkv", monitored=False)], state=hold("a")); rc, st, _ = e.run()
        self.assertIn("refusing", rc); self.assertEqual(e.puts, [])
        st0 = hold("a"); st0["_n"] = 100
        e = Env([tor("z", "Z", 9)], {"z": ["z.mkv"]}, [movie(1, "a.mkv", monitored=False)], state=st0); rc, _, _ = e.run()
        self.assertIn("still loading", rc); self.assertEqual(e.puts, [])

    def test_empty_radarr_with_holds_aborts(self):
        e = Env([tor("z", "Z", 9)], {"z": ["z.mkv"]}, [], state=hold("a")); rc, st, _ = e.run()
        self.assertIn("no movies", rc); self.assertEqual(e.puts, [])

    def test_corrupt_state_aborts_and_is_not_overwritten(self):
        e = Env([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")], raw_state="{bad")
        with self.assertRaises(ValueError):
            e.run()
        self.assertEqual(open(e.sp).read(), "{bad"); self.assertEqual(e.puts, [])

    def test_state_without_expected_keys_is_tolerated(self):
        e = Env([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")], state={"radarr": {}}); rc, st, _ = e.run()
        self.assertEqual(len(e.puts), 1)

    def test_failed_update_keeps_state_consistent_and_exit_code_nonzero(self):
        e = Env([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")], put_error=RequestException("boom"))
        rc, st, out = e.run()
        self.assertEqual(st["radarr"], {}); self.assertIn("FAILED", out); self.assertEqual(rc, 1)

    def test_a_crash_mid_apply_still_leaves_earlier_holds_on_record(self):
        e = Env([tor("a", "A", 1), tor("b", "B", 1)], {"a": ["a.mkv"], "b": ["b.mkv"]}, [movie(1, "a.mkv"), movie(2, "b.mkv")])
        calls = {"n": 0}
        def put(app, path, body):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyError("unexpected")
            e.puts.append((app, path, body.get("monitored")))
        with mock.patch.object(hg, "arr_put", side_effect=put), mock.patch.object(hg, "qbit_session", return_value=FakeQ(e.torrents, e.files)), \
                mock.patch.object(hg, "arr_get", side_effect=e.arr_get), mock.patch.object(hg, "STATE", e.sp), mock.patch.object(hg, "LOCK", os.path.join(e.tmp, "lock")), \
                mock.patch.object(hg, "EXTRA", "/nonexistent"), mock.patch.object(sys, "argv", ["x", "--apply"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyError):
                hg.main()
        self.assertIn("1", json.load(open(e.sp))["radarr"])

    def test_sonarr_failure_does_not_stop_radarr_protection(self):
        e = Env([tor("a", "A", 1)], {"a": ["a.mkv"]}, [movie(1, "a.mkv")], sonarr_series=RequestException("sonarr down"))
        rc, st, out = e.run()
        self.assertEqual(len(e.puts), 1); self.assertEqual(rc, 1); self.assertIn("FAILED reading Sonarr", out)

    def test_hold_for_a_movie_that_no_longer_exists_is_forgotten(self):
        e = Env([tor("z", "Z", 9)], {"z": ["z.mkv"]}, [movie(2, "z.mkv", monitored=False)], state=hold("a")); rc, st, _ = e.run()
        self.assertEqual(st["radarr"], {}); self.assertEqual(e.puts, [])

    def test_release_ok_function(self):
        young = {"y"}
        self.assertEqual(hg.release_ok({"hashes": ["a"]}, {"a": 1}, young), (True, {}))
        self.assertEqual(hg.release_ok({"hashes": ["y"]}, {"y": 1}, young), (False, {}))
        self.assertEqual(hg.release_ok({"hashes": ["a", "b"]}, {"a": 1}, young), (False, {"b": 1}))
        # absence is counted per hash now (the single int could not say WHICH hash had been absent): see test_review_c_py C8
        self.assertEqual(hg.release_ok({"hashes": ["a", "b"], "absent": {"b": 1}}, {"a": 1}, young), (True, {"b": 2}))
        self.assertEqual(hg.release_ok({"hashes": ["a", "b"], "absent": 1}, {"a": 1}, young), (False, {"b": 1}))      # legacy int: not attributable
        self.assertEqual(hg.release_ok({"hashes": ["a", "y"], "absent": {"a": 5}}, {"y": 1}, young), (False, {"a": 6}))


if __name__ == "__main__":
    unittest.main()
