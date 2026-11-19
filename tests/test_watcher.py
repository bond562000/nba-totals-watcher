"""Offline tests for the NBA totals watcher.

The Odds API, BALLDONTLIE, and Telegram are mocked. Nothing here spends a credit
or posts a message.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import watcher  # noqa: E402


def _team(full, name=None):
    return {"full_name": full, "name": name or full.split()[-1]}


def _raw(gid, away, home, a, h, period, clock, state, tip, status=None):
    return {
        "id": gid,
        "visitor_team": _team(away),
        "home_team": _team(home),
        "visitor_team_score": a,
        "home_team_score": h,
        "period": period,
        "time": clock,
        "status": status or "",
        "status_state": state,
        "datetime": tip,
    }


KNICKS_TIP = "2026-11-18T00:30:00Z"  # 7:30 PM EST


class WatcherTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nba-watch-")
        self._env = {}
        for key in ("NBA_STATE_DIR", "NBA_LOG_PATH", "NBA_SLOT", "NBA_BASE",
                    "NBA_MAX_POLLS", "NBA_MAX_RUNTIME_S", "NBA_SKIP_BOX_SCORES",
                    "BALLDONTLIE_API_KEY", "TELEGRAM_BOT_TOKEN", "ODDS_API_KEY",
                    "TELEGRAM_CHANNEL_ID"):
            self._env[key] = os.environ.get(key)
        os.environ["NBA_STATE_DIR"] = self.tmp
        os.environ["NBA_LOG_PATH"] = os.path.join(self.tmp, "watcher.log")
        os.environ["NBA_SLOT"] = "manual"
        os.environ["NBA_SKIP_BOX_SCORES"] = "1"
        os.environ.pop("NBA_MAX_POLLS", None)
        os.environ.pop("NBA_MAX_RUNTIME_S", None)
        watcher.DRY_RUN = False
        watcher._LOGGED_ONCE.clear()

    def tearDown(self):
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        watcher.DRY_RUN = False
        watcher._LOGGED_ONCE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, **kw):
        base = dict(dry_run=False, check=False, simulate=False, slate_preview=False)
        base.update(kw)
        return type("A", (), base)

    def test_redact_strips_odds_key_and_bot_token(self):
        dirty = ("GET https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
                 "?apiKey=SUPERSECRET&regions=us "
                 "https://api.telegram.org/bot123456:ABC-DEF_xyz/sendMessage "
                 "Authorization: Bearer hide-me")
        clean = watcher.redact(dirty)
        self.assertNotIn("SUPERSECRET", clean)
        self.assertNotIn("ABC-DEF", clean)
        self.assertNotIn("hide-me", clean)
        self.assertIn("apiKey=REDACTED", clean)
        self.assertIn("/botREDACTED", clean)

    def test_slot_hour_follows_eastern_time_across_dst(self):
        # 16:05 UTC is noon EDT and 11:05 AM EST. Only the noon hour may run the slate.
        july = datetime(2026, 7, 15, 16, 5, tzinfo=timezone.utc).astimezone(watcher.ET)
        january = datetime(2026, 1, 15, 16, 5, tzinfo=timezone.utc).astimezone(watcher.ET)
        self.assertEqual(july.hour, 12)
        self.assertEqual(july.utcoffset(), timedelta(hours=-4))
        self.assertTrue(watcher.slot_hour_ok("slate", july))
        self.assertEqual(january.hour, 11)
        self.assertEqual(january.utcoffset(), timedelta(hours=-5))
        self.assertFalse(watcher.slot_hour_ok("slate", january))

        jan_noon = datetime(2026, 1, 15, 17, 5, tzinfo=timezone.utc).astimezone(watcher.ET)
        self.assertEqual(jan_noon.hour, 12)
        self.assertTrue(watcher.slot_hour_ok("slate", jan_noon))
        # The other offset during EST is 1:05 PM, which must no-op.
        jan_wrong = datetime(2026, 1, 15, 18, 5, tzinfo=timezone.utc).astimezone(watcher.ET)
        self.assertEqual(jan_wrong.hour, 13)
        self.assertFalse(watcher.slot_hour_ok("slate", jan_wrong))

    def test_evening_and_late_grace_rejects_the_other_offset(self):
        # 5:45 PM is the evening slot. 6:20 PM still counts (backup / late cron).
        # 6:45 PM is where the other DST offset lands, and it must no-op.
        evening = datetime(2026, 11, 18, 17, 45, tzinfo=watcher.ET)
        self.assertTrue(watcher.slot_hour_ok("evening", evening))
        self.assertTrue(watcher.slot_hour_ok("evening", evening.replace(minute=40)))
        self.assertFalse(watcher.slot_hour_ok("evening", evening.replace(minute=20)))
        self.assertTrue(watcher.slot_hour_ok("evening", evening.replace(hour=18, minute=20)))
        self.assertFalse(watcher.slot_hour_ok("evening", evening.replace(hour=18, minute=45)))
        self.assertFalse(watcher.slot_hour_ok("evening", evening.replace(hour=16, minute=45)))

        late = datetime(2026, 11, 18, 23, 20, tzinfo=watcher.ET)
        self.assertTrue(watcher.slot_hour_ok("late", late))
        after_midnight = datetime(2026, 11, 19, 0, 10, tzinfo=watcher.ET)
        self.assertTrue(watcher.slot_hour_ok("late", after_midnight))
        # Other DST offset of the 11:20 PM cron lands at 12:20 AM.
        self.assertFalse(watcher.slot_hour_ok("late", after_midnight.replace(minute=20)))
        self.assertTrue(watcher.slot_hour_ok("manual", evening.replace(hour=3)))
        self.assertFalse(watcher.slot_hour_ok("unknown", evening))

    def test_wrong_hour_exits_before_any_api_call(self):
        os.environ["NBA_SLOT"] = "slate"
        os.environ.pop("BALLDONTLIE_API_KEY", None)

        def fail_clock():
            return datetime(2026, 1, 15, 11, 5, tzinfo=watcher.ET)

        watcher.now_et = fail_clock
        called = {"n": 0}

        def boom(*_a, **_k):
            called["n"] += 1
            raise AssertionError("network")

        orig = watcher.http_get
        watcher.http_get = boom
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watcher.run(self._args())
        finally:
            watcher.http_get = orig
            watcher.now_et = _real_now
        self.assertEqual(called["n"], 0)
        self.assertIn("no-op", buf.getvalue())
        self.assertNotIn("apiKey", buf.getvalue())

    def test_pregame_reuse_logs_once(self):
        st = {"slate": "2026-11-18", "pregame": {"knicks@raptors": 226.5},
              "pregame_done": True, "slate_posted": True, "odds_calls": 1,
              "credits_remaining": 100, "games": {}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            for _ in range(5):
                watcher.reuben_pregame(st, "SHOULD-NOT-BE-USED", [])
        text = buf.getvalue()
        self.assertEqual(text.count("reusing saved pregame totals"), 1)
        self.assertIn("for 1 games", text)
        self.assertNotIn("SHOULD-NOT-BE-USED", text)
        self.assertEqual(st["odds_calls"], 1)

    def test_quota_guards_do_not_call_odds(self):
        def fail_http(*_a, **_k):
            raise AssertionError("Odds API was called")

        orig = watcher.http_get
        watcher.http_get = fail_http
        try:
            capped = {"odds_calls": watcher.MAX_ODDS_CALLS_PER_NIGHT, "credits_remaining": 500}
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertIsNone(watcher.reuben_fetch(capped, "SECRET", "live"))
            self.assertIn("nightly cap", buf.getvalue())
            self.assertNotIn("SECRET", buf.getvalue())
            self.assertEqual(capped["odds_calls"], watcher.MAX_ODDS_CALLS_PER_NIGHT)

            poor = {"odds_calls": 3, "credits_remaining": watcher.MIN_CREDITS - 1}
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertIsNone(watcher.reuben_fetch(poor, "SECRET", "live"))
            self.assertEqual(poor["odds_calls"], 3)
            self.assertIn("credits left", buf.getvalue())

            # Exactly 25 credits is still enough to call.
            ok = {"odds_calls": 0, "credits_remaining": watcher.MIN_CREDITS, "slate": "2026-11-18"}
        finally:
            watcher.http_get = orig

        captured = {}

        class Resp:
            headers = {"x-requests-remaining": "25", "x-requests-used": "1"}

            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            return Resp()

        orig_open = watcher.urllib.request.urlopen
        watcher.urllib.request.urlopen = fake_urlopen
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                lines = watcher.reuben_fetch(ok, "SUPERSECRET", "pregame snapshot")
        finally:
            watcher.urllib.request.urlopen = orig_open
        self.assertEqual(lines, {})
        self.assertIn("apiKey=SUPERSECRET", captured["url"])
        self.assertNotIn("SUPERSECRET", buf.getvalue())
        log_text = Path(os.environ["NBA_LOG_PATH"]).read_text()
        self.assertNotIn("SUPERSECRET", log_text)
        self.assertEqual(ok["odds_calls"], 1)
        self.assertEqual(ok["credits_remaining"], 25)

    def test_odds_http_error_does_not_log_url(self):
        def fake_urlopen(req, timeout=20):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

        st = {"odds_calls": 0, "credits_remaining": 100, "slate": "2026-11-18"}
        orig = watcher.urllib.request.urlopen
        watcher.urllib.request.urlopen = fake_urlopen
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertIsNone(watcher.reuben_fetch(st, "SUPERSECRET", "pregame snapshot"))
        finally:
            watcher.urllib.request.urlopen = orig
        self.assertNotIn("SUPERSECRET", buf.getvalue())
        self.assertNotIn("api.the-odds-api.com", buf.getvalue())
        self.assertIn("HTTP 401", buf.getvalue())
        self.assertEqual(st["odds_calls"], 1)

    def test_trigger_window_and_frank_rules(self):
        # Alert window is Q4 with 8:00 through 5:00 left. Q3 is logged, not alerted.
        q3 = watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 3, "6:42",
            "in_progress", KNICKS_TIP, "3rd Qtr"))
        self.assertFalse(q3["in_window"])
        inside = watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 4, "6:30",
            "in_progress", KNICKS_TIP, "4th Qtr"))
        self.assertTrue(inside["in_window"])
        self.assertTrue(watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 4, "8:00",
            "in_progress", KNICKS_TIP, "4th Qtr"))["in_window"])
        self.assertTrue(watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 4, "5:00",
            "in_progress", KNICKS_TIP, "4th Qtr"))["in_window"])
        self.assertFalse(watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 4, "9:30",
            "in_progress", KNICKS_TIP, "4th Qtr"))["in_window"])
        self.assertFalse(watcher.scout(_raw(
            1, "New York Knicks", "Toronto Raptors", 110, 104, 4, "4:00",
            "in_progress", KNICKS_TIP, "4th Qtr"))["in_window"])
        for clock, name in (("8:00", "q4_8:00"), ("6:30", "q4_6:30"), ("5:00", "q4_5:00"), ("2:00", "q4_2:00")):
            sample = watcher.scout(_raw(
                9, "Boston Celtics", "Miami Heat", 100, 98, 4, clock,
                "in_progress", KNICKS_TIP, "4th Qtr"))
            self.assertEqual([n for n, _ in watcher.checkpoint_names(sample, {})], [name])
        self.assertEqual(watcher.checkpoint_names(watcher.scout(_raw(
            9, "Boston Celtics", "Miami Heat", 90, 88, 4, "9:30",
            "in_progress", KNICKS_TIP, "4th Qtr")), {}), [])
        half = watcher.scout(_raw(
            9, "Boston Celtics", "Miami Heat", 55, 52, 2, "Halftime",
            "in_progress", KNICKS_TIP, "Halftime"))
        self.assertEqual([n for n, _ in watcher.checkpoint_names(half, {})], ["halftime"])
        end_q3 = watcher.scout(_raw(
            9, "Boston Celtics", "Miami Heat", 80, 78, 3, "0:00",
            "in_progress", KNICKS_TIP, "End of 3rd"))
        self.assertEqual([n for n, _ in watcher.checkpoint_names(end_q3, {})], ["end_q3"])

        res = watcher.yen_and_frank(inside, {"total": 229.5, "over": -110, "under": -110})
        self.assertGreaterEqual(abs(res["edge"]), watcher.MIN_EDGE)
        self.assertEqual(res["verdict"], "OVER")
        self.assertEqual(res["model"], "pace_fallback")
        self.assertEqual(res["calibration"], "UNCALIBRATED")
        self.assertEqual(res["settlement"], "full_game_including_ot")
        card = watcher.dell_format(inside, {"total": 229.5, "over": -110, "under": -110}, res, 226.5)
        self.assertIn("FINAL CALL: <b>OVER 229.5</b>", card)
        self.assertIn("Total <b>229.5</b>", card)
        self.assertIn("Projected Total: <b>", card)
        self.assertIn("New York Knicks @ Toronto Raptors", card)
        self.assertIn("opened 226.5", card)
        self.assertIn("UNCALIBRATED", card)

        close_under = watcher.scout(_raw(
            2, "Detroit Pistons", "San Antonio Spurs", 90, 94, 4, "6:00",
            "in_progress", "2026-11-18T01:00:00Z", "4th Qtr"))
        res = watcher.yen_and_frank(close_under, {"total": 230.5, "over": -112, "under": -108})
        self.assertLess(res["edge"], 0)
        self.assertEqual(res["verdict"], "PASS / NO PLAY")
        self.assertIn("close-game fouling", res["reason"])

        blowout = watcher.scout(_raw(
            3, "Utah Jazz", "LA Clippers", 70, 100, 4, "6:00",
            "in_progress", "2026-11-18T03:30:00Z", "4th Qtr"))
        self.assertGreaterEqual(blowout["margin"], watcher.BLOWOUT_MARGIN)
        res = watcher.yen_and_frank(blowout, {"total": 220.5, "over": -105, "under": -115})
        self.assertEqual(res["verdict"], "UNDER")
        self.assertTrue(res["blowout"])
        # Same spot with a total low enough that the side would be OVER: Frank passes.
        forced = watcher.yen_and_frank(blowout, {"total": 180.5, "over": -105, "under": -115})
        self.assertGreater(forced["edge"], 0)
        self.assertEqual(forced["verdict"], "PASS / NO PLAY")
        self.assertIn("blowout contradicts OVER", forced["reason"])

        def _edge_line(game, edge):
            base = watcher.yen_and_frank(game, {"total": 0, "over": -110, "under": -110})
            return {"total": base["proj"] - edge, "over": -110, "under": -110}

        margin21 = watcher.scout(_raw(
            8, "Boston Celtics", "Miami Heat", 80, 101, 4, "6:30",
            "in_progress", KNICKS_TIP, "4th Qtr"))
        self.assertEqual(margin21["margin"], 21)
        played = watcher.yen_and_frank(margin21, _edge_line(margin21, 4.0))
        self.assertEqual(played["tier"], "play")
        self.assertEqual(played["verdict"], "OVER")
        margin22 = watcher.scout(_raw(
            8, "Boston Celtics", "Miami Heat", 80, 102, 4, "6:30",
            "in_progress", KNICKS_TIP, "4th Qtr"))
        vetoed = watcher.yen_and_frank(margin22, _edge_line(margin22, 4.0))
        self.assertEqual(vetoed["verdict"], "PASS / NO PLAY")
        self.assertIn("blowout contradicts OVER", vetoed["reason"])

        lean = watcher.yen_and_frank(inside, _edge_line(inside, 3.2))
        self.assertEqual(lean["tier"], "lean")
        self.assertIn("⚠️ LEAN: <b>OVER", watcher.dell_format(inside, {"total": lean["proj"] - 3.2, "over": -110, "under": -110}, lean))
        soft = watcher.yen_and_frank(inside, _edge_line(inside, 2.9))
        self.assertIsNone(soft["tier"])
        self.assertEqual(soft["verdict"], "PASS / NO PLAY")

        late_q3 = watcher.scout(_raw(
            8, "Boston Celtics", "Miami Heat", 90, 88, 3, "1:30",
            "in_progress", KNICKS_TIP, "3rd Qtr"))
        self.assertTrue(late_q3["in_q3_late"])
        self.assertFalse(late_q3["in_window"])
        self.assertEqual([n for n, _ in watcher.checkpoint_names(watcher.scout(_raw(
            8, "Boston Celtics", "Miami Heat", 90, 88, 3, "2:00",
            "in_progress", KNICKS_TIP, "3rd Qtr")), {})], ["q3_2:00"])
        q3_lean = watcher.yen_and_frank(late_q3, _edge_line(late_q3, 4.6))
        self.assertEqual(q3_lean["tier"], "lean")
        q3_play = watcher.yen_and_frank(late_q3, _edge_line(late_q3, 5.2))
        self.assertEqual(q3_play["tier"], "play")
        q3_pass = watcher.yen_and_frank(late_q3, _edge_line(late_q3, 4.2))
        self.assertEqual(q3_pass["verdict"], "PASS / NO PLAY")

    def test_dry_run_posts_once_and_mocks_odds(self):
        """End-to-end dry run: scripted scoreboard, mocked Odds API, no Telegram."""
        scheduled = watcher.scout(_raw(
            11, "New York Knicks", "Toronto Raptors", 0, 0, 0, None,
            "scheduled", KNICKS_TIP))
        live = watcher.scout(_raw(
            11, "New York Knicks", "Toronto Raptors", 110, 104, 4, "6:30",
            "in_progress", KNICKS_TIP, "4th Qtr",
        ))
        live["box"] = {
            "away": {"fga": 78, "fta": 22, "oreb": 10, "tov": 12, "pts": 110},
            "home": {"fga": 76, "fta": 20, "oreb": 9, "tov": 11, "pts": 104},
        }
        cold = watcher.scout(_raw(
            11, "New York Knicks", "Toronto Raptors", 110, 104, 4, "1:00",
            "in_progress", KNICKS_TIP, "4th Qtr"))
        final = watcher.scout(_raw(
            11, "New York Knicks", "Toronto Raptors", 110, 108, 4, "Final",
            "final", KNICKS_TIP, "Final"))
        # Hot Q4 sample posts the OVER. The cold 1:00 sample is outside the alert
        # window, so it must not spend another Odds API call, and it should buy out.
        polls = iter([[scheduled], [live], [cold], [final]])

        def fake_poll(_key, _sd):
            return next(polls)

        odds_calls = {"n": 0}
        urls = []

        def fake_fetch(st, odds_key, why):
            self.assertNotEqual(odds_key, "")
            odds_calls["n"] += 1
            st["odds_calls"] = st.get("odds_calls", 0) + 1
            st["credits_remaining"] = 400
            watcher.save_state(st)
            if why.startswith("pregame"):
                return {watcher.game_key(scheduled["away"], scheduled["home"]):
                        {"total": 226.5, "over": -110, "under": -110}}
            return {watcher.game_key(live["away"], live["home"]):
                    {"total": 229.5, "over": -110, "under": -110}}

        def forbidden_urlopen(req, timeout=20):
            urls.append(getattr(req, "full_url", str(req)))
            raise AssertionError(f"unexpected network: {getattr(req, 'full_url', req)}")

        fixed = datetime(2026, 11, 18, 20, 0, tzinfo=watcher.ET)
        origs = (watcher.rusty_poll, watcher.reuben_fetch, watcher.urllib.request.urlopen,
                 watcher.now_et, watcher.time.sleep)
        watcher.rusty_poll = fake_poll
        watcher.reuben_fetch = fake_fetch
        watcher.urllib.request.urlopen = forbidden_urlopen
        watcher.now_et = lambda: fixed
        watcher.time.sleep = lambda _s: None
        watcher.DRY_RUN = True
        os.environ["BALLDONTLIE_API_KEY"] = "test-bdl"
        os.environ["ODDS_API_KEY"] = "test-odds"
        os.environ["TELEGRAM_CHANNEL_ID"] = "@test-channel"
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watcher.run(self._args(dry_run=True))
            text = buf.getvalue()
        finally:
            (watcher.rusty_poll, watcher.reuben_fetch, watcher.urllib.request.urlopen,
             watcher.now_et, watcher.time.sleep) = origs

        self.assertEqual(urls, [])
        self.assertEqual(odds_calls["n"], 2)  # one pregame, one live
        self.assertEqual(text.count("reusing saved pregame totals"), 1)
        self.assertEqual(text.count("DRY RUN, would post"), 3)  # slate + one pick + one buyout
        self.assertEqual(text.count("FINAL CALL:"), 1)
        self.assertIn("FINAL CALL: <b>OVER 229.5</b>", text)
        self.assertIn("possession_estimate", text)
        self.assertIn("checkpoint q4_6:30", text)
        self.assertIn("TAKE THE BUYOUT", text)
        self.assertIn("Original call: <b>OVER 229.5</b>", text)
        self.assertIn("Tonight's NBA Slate", text)
        self.assertNotIn("api.telegram.org", text)
        state = json.loads(Path(self.tmp, "state-2026-11-18-dryrun.json").read_text())
        self.assertTrue(state["slate_posted"])
        self.assertTrue(state["pregame_done"])
        self.assertEqual(state["pregame"]["knicks@raptors"], 226.5)
        self.assertEqual(state["odds_calls"], 2)
        self.assertTrue(state["games"]["11"]["posted"])
        self.assertTrue(state["games"]["11"]["buyout_posted"])
        self.assertEqual(state["games"]["11"]["evals"], 1)
        # The pregame credit is recorded on the live file; the posted flags are not.
        live = json.loads(Path(self.tmp, "state-2026-11-18.json").read_text())
        self.assertEqual(live["pregame"]["knicks@raptors"], 226.5)
        self.assertTrue(live["pregame_done"])
        self.assertEqual(live["odds_calls"], 2)
        self.assertFalse(live["slate_posted"])
        self.assertEqual(live["games"], {})
        # The live run later that night reuses the snapshot and does not call Odds.
        watcher.DRY_RUN = False
        watcher._LOGGED_ONCE.clear()
        live_st = watcher.load_state(datetime.fromisoformat("2026-11-18").date())
        self.assertTrue(live_st["pregame_done"])
        again = io.StringIO()
        with redirect_stdout(again):
            watcher.reuben_pregame(live_st, "SUPERSECRET", [])
        self.assertIn("reusing saved pregame totals", again.getvalue())
        self.assertNotIn("SUPERSECRET", again.getvalue())
        self.assertEqual(live_st["odds_calls"], 2)

    def test_second_eval_and_cap(self):
        g = watcher.scout(_raw(
            7, "Boston Celtics", "Miami Heat", 80, 90, 3, "5:00",
            "in_progress", KNICKS_TIP, "3rd Qtr"))
        # Line sits on the projection so the edge is ~0 and Frank passes (no post).
        # A second eval is allowed after the gap, and a third is not.
        proj = watcher.yen_and_frank(g, {"total": 0, "over": -110, "under": -110})["proj"]
        line = {"total": proj, "over": -110, "under": -110}
        st = {"slate": "2026-11-18", "pregame": {}, "pregame_done": True,
              "slate_posted": True, "odds_calls": 1, "credits_remaining": 80, "games": {}}
        watcher.DRY_RUN = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            watcher.evaluate(g, line, st, "", "")
        self.assertEqual(st["games"]["7"]["evals"], 1)
        self.assertNotIn("posted", st["games"]["7"])
        self.assertFalse(watcher.due_for_eval(st["games"]["7"], time_now := __import__("time").time()))
        self.assertTrue(watcher.due_for_eval(st["games"]["7"], time_now + watcher.REEVAL_GAP_S))
        with redirect_stdout(io.StringIO()):
            watcher.evaluate(g, line, st, "", "")
        self.assertEqual(st["games"]["7"]["evals"], 2)
        self.assertFalse(watcher.due_for_eval(st["games"]["7"], __import__("time").time() + 10 * watcher.REEVAL_GAP_S))

    def test_slate_job_exits_when_only_evening_games_remain(self):
        evening = watcher.scout(_raw(
            21, "Los Angeles Lakers", "Golden State Warriors", 0, 0, 0, None,
            "scheduled", "2026-11-19T02:00:00Z"))  # 9:00 PM EST
        polls = {"n": 0}

        def fake_poll(_key, _sd):
            polls["n"] += 1
            return [evening]

        fetches = {"n": 0}

        def fake_fetch(st, _key, why):
            fetches["n"] += 1
            st["odds_calls"] += 1
            st["credits_remaining"] = 300
            watcher.save_state(st)
            return {watcher.game_key(evening["away"], evening["home"]):
                    {"total": 224.5, "over": -110, "under": -110}}

        noon = datetime(2026, 11, 18, 12, 10, tzinfo=watcher.ET)
        origs = (watcher.rusty_poll, watcher.reuben_fetch, watcher.now_et, watcher.time.sleep)
        watcher.rusty_poll = fake_poll
        watcher.reuben_fetch = fake_fetch
        watcher.now_et = lambda: noon
        sleeps = {"n": 0}
        watcher.time.sleep = lambda _s: sleeps.__setitem__("n", sleeps["n"] + 1)
        watcher.DRY_RUN = True
        os.environ["NBA_SLOT"] = "slate"
        os.environ["BALLDONTLIE_API_KEY"] = "test-bdl"
        os.environ["ODDS_API_KEY"] = "test-odds"
        os.environ["TELEGRAM_CHANNEL_ID"] = "@c"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watcher.run(self._args(dry_run=True))
            text = buf.getvalue()
        finally:
            (watcher.rusty_poll, watcher.reuben_fetch, watcher.now_et, watcher.time.sleep) = origs
        self.assertEqual(polls["n"], 1)
        self.assertEqual(fetches["n"], 1)
        self.assertEqual(sleeps["n"], 0)
        self.assertIn("no afternoon games left to follow", text)
        self.assertIn("DRY RUN, would post", text)
        self.assertIn("slate posted", text)

    def test_slate_keeps_polling_a_live_afternoon_game_until_handoff(self):
        live = watcher.scout(_raw(
            31, "New York Knicks", "Toronto Raptors", 40, 38, 2, "6:00",
            "in_progress", "2026-11-18T18:00:00Z", "2nd Qtr"))
        self.assertFalse(live["in_window"])  # Q2 is never a trigger
        # Starts during the noon hour (the gate), then the clock walks through
        # the afternoon while the same process keeps polling.
        clock = {"now": datetime(2026, 11, 18, 12, 10, tzinfo=watcher.ET)}

        def fake_poll(_key, _sd):
            return [live]

        def fake_fetch(st, _key, _why):
            st["odds_calls"] += 1
            st["credits_remaining"] = 200
            return {}

        def advance(_s):
            clock["now"] = clock["now"] + timedelta(minutes=30)

        origs = (watcher.rusty_poll, watcher.reuben_fetch, watcher.now_et, watcher.time.sleep)
        watcher.rusty_poll = fake_poll
        watcher.reuben_fetch = fake_fetch
        watcher.now_et = lambda: clock["now"]
        watcher.time.sleep = advance
        watcher.DRY_RUN = True
        os.environ["NBA_SLOT"] = "slate"
        os.environ["BALLDONTLIE_API_KEY"] = "test-bdl"
        os.environ["ODDS_API_KEY"] = "test-odds"
        os.environ["TELEGRAM_CHANNEL_ID"] = "@c"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watcher.run(self._args(dry_run=True))
            text = buf.getvalue()
        finally:
            (watcher.rusty_poll, watcher.reuben_fetch, watcher.now_et, watcher.time.sleep) = origs
        self.assertIn("handing off", text)
        self.assertGreaterEqual((clock["now"].hour, clock["now"].minute), watcher.SLATE_HANDOFF)
        # Pregame sees no games still scheduled, so it must not call the Odds API.
        state = json.loads(Path(self.tmp, "state-2026-11-18-dryrun.json").read_text())
        self.assertTrue(state["pregame_done"])
        self.assertEqual(state["odds_calls"], 0)

    def test_workflow_crons_hit_one_slot_in_one_season(self):
        """Each UTC cron is a real slot in either EST or EDT, and not both seasons
        except when the off-season instant is still inside that same slot's window."""
        text = (ROOT / ".github" / "workflows" / "nba-totals.yml").read_text()
        crons = re.findall(r"- cron: '([^']+)'", text)
        self.assertGreaterEqual(len(crons), 6)
        slots = ("slate", "evening", "late")

        def accepted(cron, month):
            minute, hour, *_ = cron.split()
            utc = datetime(2026, month, 15, int(hour), int(minute), tzinfo=timezone.utc)
            et = utc.astimezone(watcher.ET)
            return [s for s in slots if watcher.slot_hour_ok(s, et)]

        for cron in crons:
            jan = accepted(cron, 1)
            jul = accepted(cron, 7)
            self.assertLessEqual(len(jan), 1, cron)
            self.assertLessEqual(len(jul), 1, cron)
            self.assertTrue(jan or jul, cron)
            if jan and jul:
                self.assertEqual(jan, jul, cron)

    def test_posted_game_is_not_evaluated_again(self):
        self.assertFalse(watcher.due_for_eval({"posted": True, "evals": 1}, 10**12))

    def test_simulate_is_offline_and_covers_each_rule(self):
        def boom(*_a, **_k):
            raise AssertionError("simulate tried to use the network")

        orig = watcher.urllib.request.urlopen
        watcher.urllib.request.urlopen = boom
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                watcher.simulate()
            text = buf.getvalue()
        finally:
            watcher.urllib.request.urlopen = orig
        self.assertIn("no Odds API / Telegram / BALLDONTLIE", text)
        self.assertIn("FINAL CALL: <b>OVER 229.5</b>", text)
        self.assertIn("FINAL CALL: <b>UNDER 220.5</b>", text)
        self.assertIn("TAKE THE BUYOUT", text)
        self.assertIn("checkpoint: q4_6:30", text)
        self.assertIn("UNCALIBRATED", text)
        self.assertIn("PASS / NO PLAY", text)
        self.assertIn("close-game fouling contradicts UNDER", text)
        self.assertIn("not in trigger window", text)
        self.assertIn("Tonight's NBA Slate (Nov 18)", text)
        self.assertNotIn("apiKey", text)

    def test_cli_simulate_subprocess(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "watcher.py"), "--simulate"],
            check=True, capture_output=True, text=True, cwd=ROOT,
        )
        self.assertIn("FINAL CALL: <b>OVER 229.5</b>", proc.stdout)
        self.assertIn("FINAL CALL: <b>UNDER 220.5</b>", proc.stdout)
        self.assertIn("TAKE THE BUYOUT", proc.stdout)
        self.assertIn("= 207", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_state_branch_roundtrip(self):
        script_persist = ROOT / "scripts" / "persist_state.sh"
        script_restore = ROOT / "scripts" / "restore_state.sh"
        work = Path(self.tmp) / "repo"
        bare = Path(self.tmp) / "bare.git"
        state_a = Path(self.tmp) / "state-a"
        state_b = Path(self.tmp) / "state-b"
        work.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "NBA_STATE_LOCK": str(Path(self.tmp) / "lock"),
        })
        subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True, env=env)
        (work / "README.md").write_text("code\n")
        subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True, env=env)
        subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True, env=env)
        subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True, env=env)
        head_before = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()

        state_a.mkdir()
        payload = {
            "slate": "2026-11-18",
            "pregame": {"knicks@raptors": 226.5},
            "pregame_done": True,
            "slate_posted": True,
            "odds_calls": 1,
            "credits_remaining": 99,
            "games": {"11": {"posted": True, "evals": 1}},
        }
        (state_a / "state-2026-11-18.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        # A non-state file in the directory must not be published.
        (state_a / "odds_api_key").write_text("do-not-commit\n")
        env["NBA_STATE_DIR"] = str(state_a)
        subprocess.run(["bash", str(script_persist)], check=True, cwd=work, env=env)
        # Unchanged JSON does not create another commit.
        subprocess.run(["bash", str(script_persist)], check=True, cwd=work, env=env)
        log = subprocess.check_output(
            ["git", "-C", str(work), "log", "--oneline", "origin/state"], text=True).strip().splitlines()
        self.assertEqual(len(log), 1)
        self.assertIn("2026-11-18", log[0])

        listed = subprocess.check_output(
            ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "origin/state"], text=True)
        self.assertEqual(listed.strip(), "state-2026-11-18.json")
        blob = subprocess.check_output(
            ["git", "-C", str(work), "show", "origin/state:state-2026-11-18.json"], text=True)
        self.assertNotIn("do-not-commit", blob)
        self.assertEqual(json.loads(blob)["games"]["11"]["posted"], True)

        # Working branch is untouched.
        head_after = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(head_before, head_after)
        branch = subprocess.check_output(["git", "-C", str(work), "branch", "--show-current"], text=True).strip()
        self.assertEqual(branch, "main")

        env["NBA_STATE_DIR"] = str(state_b)
        subprocess.run(["bash", str(script_restore)], check=True, cwd=work, env=env)
        restored = json.loads((state_b / "state-2026-11-18.json").read_text())
        self.assertEqual(restored["pregame"]["knicks@raptors"], 226.5)
        self.assertEqual(restored["odds_calls"], 1)

        # A second night's file appends a commit and keeps the previous slate.
        payload["odds_calls"] = 4
        (state_a / "state-2026-11-19.json").write_text("{}\n")
        (state_a / "state-2026-11-18.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        env["NBA_STATE_DIR"] = str(state_a)
        subprocess.run(["bash", str(script_persist)], check=True, cwd=work, env=env)
        names = subprocess.check_output(
            ["git", "-C", str(work), "ls-tree", "-r", "--name-only", "origin/state"], text=True)
        self.assertEqual(sorted(names.split()), ["state-2026-11-18.json", "state-2026-11-19.json"])
        updated = json.loads(subprocess.check_output(
            ["git", "-C", str(work), "show", "origin/state:state-2026-11-18.json"], text=True))
        self.assertEqual(updated["odds_calls"], 4)


# Captured before any test replaces watcher.now_et.
_real_now = watcher.now_et


if __name__ == "__main__":
    unittest.main()
