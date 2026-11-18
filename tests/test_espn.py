"""ESPN box matching. HTTP is mocked; nothing here calls ESPN."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import espn_box  # noqa: E402
import model  # noqa: E402
import watcher  # noqa: E402


def _summary(clock="6:30", away=111, home=102, state="in", completed=False):
    desc = "Final" if completed else "4th Quarter"
    return {
        "header": {"competitions": [{
            "id": "99",
            "status": {"period": 4, "displayClock": clock, "type": {
                "state": "post" if completed else state,
                "completed": completed,
                "description": desc,
                "detail": desc,
            }},
            "competitors": [
                {"homeAway": "away", "score": str(away), "possession": False,
                 "team": {"id": "18", "displayName": "New York Knicks"},
                 "linescores": [{"value": 23}, {"value": 31}, {"value": 26}, {"value": 31}]},
                {"homeAway": "home", "score": str(home), "possession": True,
                 "team": {"id": "28", "displayName": "Toronto Raptors"},
                 "linescores": [{"value": 29}, {"value": 21}, {"value": 33}, {"value": 19}]},
            ],
        }]},
        "boxscore": {"teams": [
            {"homeAway": "away", "team": {"id": "18", "displayName": "New York Knicks"}, "statistics": [
                {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "41-78"},
                {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "17-19"},
                {"name": "offensiveRebounds", "displayValue": "12"},
                {"name": "turnovers", "displayValue": "12"},
                {"name": "totalTurnovers", "displayValue": "13"},
            ]},
            {"homeAway": "home", "team": {"id": "28", "displayName": "Toronto Raptors"}, "statistics": [
                {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "39-78"},
                {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "10-18"},
                {"name": "offensiveRebounds", "displayValue": "5"},
                {"name": "totalTurnovers", "displayValue": "6"},
            ]},
        ], "players": []},
        "plays": [
            {"type": {"text": "Jump Shot"}, "text": "shot", "awayScore": 100, "homeScore": 90,
             "period": {"number": 4}, "clock": {"displayValue": "9:00"}, "shootingPlay": True,
             "team": {"id": "18"}, "pointsAttempted": 2},
            {"type": {"text": "Offensive Rebound"}, "text": "Knicks offensive team rebound",
             "awayScore": 108, "homeScore": 100, "period": {"number": 4}, "clock": {"displayValue": "7:00"},
             "shootingPlay": False, "team": {"id": "18"}},
            {"type": {"text": "Offensive Rebound"}, "text": "Towns offensive rebound",
             "awayScore": 110, "homeScore": 102, "period": {"number": 4}, "clock": {"displayValue": "6:40"},
             "shootingPlay": False, "team": {"id": "18"}},
            {"type": {"text": "Free Throw - 1 of 2"}, "text": "ft", "awayScore": 111, "homeScore": 102,
             "period": {"number": 4}, "clock": {"displayValue": "6:20"}, "team": {"id": "18"}},
            {"type": {"text": "Lost Ball Turnover"}, "text": "tov", "awayScore": 111, "homeScore": 102,
             "period": {"number": 4}, "clock": {"displayValue": "6:10"}, "team": {"id": "28"}},
            {"type": {"text": "Full Timeout"}, "text": "Raptors Full timeout", "awayScore": 111, "homeScore": 102,
             "period": {"number": 4}, "clock": {"displayValue": "6:00"}, "team": {"id": "28"}},
            {"type": {"text": "Shooting Foul"}, "text": "foul", "awayScore": 111, "homeScore": 102,
             "period": {"number": 4}, "clock": {"displayValue": "5:40"}, "team": {"id": "28"}},
        ],
    }


def _board():
    summary = _summary()
    return {"events": [{"id": "99", "competitions": summary["header"]["competitions"]}]}


def _game(score=(111, 102), clock="6:30", state="in_progress"):
    raw = {
        "id": 11,
        "visitor_team": {"full_name": "New York Knicks", "name": "Knicks"},
        "home_team": {"full_name": "Toronto Raptors", "name": "Raptors"},
        "visitor_team_score": score[0], "home_team_score": score[1],
        "period": 4, "time": clock, "status": "4th Qtr", "status_state": state,
        "datetime": "2026-11-18T00:30:00Z",
    }
    if state == "final":
        raw["status"] = "Final"
        raw["time"] = "Final"
    return watcher.scout(raw)


class EspnBoxTest(unittest.TestCase):
    def setUp(self):
        espn_box._CACHE.clear()

    def test_live_summary_feeds_the_possession_model(self):
        game = _game()
        calls = []

        def fetch(url):
            calls.append(url)
            if "scoreboard" in url:
                return _board(), 200
            return _summary(), 200

        logs = espn_box.attach_espn_boxes([game], {"games": {}}, date(2026, 11, 17), fetch=fetch)
        self.assertIn("box", game)
        # Team turnovers are included. The player-only turnover column is 12.
        self.assertEqual(game["box"]["away"]["tov"], 13)
        self.assertEqual(game["box"]["away"]["fga"], 78)
        self.assertEqual(game["box"]["away"]["oreb"], 12)
        self.assertEqual(game["box"]["home"]["fta"], 18)
        self.assertTrue(any("score and clock agree" in line for line in logs))
        self.assertNotIn("apiKey", " ".join(logs))
        pack = model.project_game(game, 226.5)
        self.assertEqual(pack["model"], "possession_estimate")
        self.assertEqual(pack["calibration"], "UNCALIBRATED")
        self.assertNotIn("team_box_fga_oreb_tov_fta", pack["missing_inputs"])
        self.assertNotIn("quarter_splits", pack["missing_inputs"])
        self.assertNotIn("possession_arrow", pack["missing_inputs"])
        self.assertIn("season_team_ratings", pack["missing_inputs"])
        self.assertIn("timeouts_remaining", pack["missing_inputs"])
        # One scoreboard, one summary. The cache stops a second poll from refetching.
        espn_box.attach_espn_boxes([_game()], {"games": {}}, date(2026, 11, 17), fetch=fetch)
        self.assertEqual(sum("scoreboard" in url for url in calls), 1)
        self.assertEqual(sum("summary" in url for url in calls), 1)

    def test_score_or_clock_mismatch_keeps_the_fallback(self):
        wrong_score = _game(score=(110, 102))

        def fetch(url):
            if "scoreboard" in url:
                return _board(), 200
            return _summary(), 200

        logs = espn_box.attach_espn_boxes([wrong_score], {"games": {}}, date(2026, 11, 17), fetch=fetch)
        self.assertNotIn("box", wrong_score)
        self.assertTrue(any("score mismatch" in line for line in logs))

        espn_box._CACHE.clear()
        wrong_clock = _game(clock="2:00")
        logs = espn_box.attach_espn_boxes([wrong_clock], {"games": {}}, date(2026, 11, 17), fetch=fetch)
        self.assertNotIn("box", wrong_clock)
        self.assertTrue(any("clock mismatch" in line for line in logs))

    def test_final_ignores_espn_dummy_clock(self):
        game = _game(state="final")
        summary = _summary(clock="12:00", completed=True)
        espn = espn_box.parse_summary(summary)["event"]
        ok, why = espn_box.situations_agree(game, espn)
        self.assertTrue(ok, why)
        self.assertIn("final scores agree", why)
        calls = []

        def fetch(url):
            calls.append(url)
            return _board(), 200

        # A slate that is already final does not spend an ESPN request.
        logs = espn_box.attach_espn_boxes([game], {"games": {}}, date(2026, 11, 17), fetch=fetch)
        self.assertEqual(calls, [])
        self.assertEqual(logs, [])
        self.assertNotIn("box", game)

    def test_rate_limit_backs_off_without_a_box(self):
        game = _game()
        st = {"games": {}}

        def fetch(url):
            return None, 429

        logs = espn_box.attach_espn_boxes([game], st, date(2026, 11, 17), fetch=fetch)
        self.assertNotIn("box", game)
        self.assertGreater(st["espn"]["backoff_until"], 0)
        self.assertTrue(any("429" in line for line in logs))
        logs = espn_box.attach_espn_boxes([game], st, date(2026, 11, 17), fetch=fetch)
        self.assertTrue(any("backoff" in line for line in logs))

    def test_team_rebound_does_not_count_as_oreb(self):
        snap = espn_box.snapshot_from_plays(_summary(), elapsed_min=41.5)
        # Only the player offensive rebound at 6:40 is at or before 6:30 left (elapsed 41.5).
        # The team rebound at 7:00 left is earlier and is ignored. The 6:40 player rebound
        # is just after 6:30 left? 6:40 left is elapsed 41 + 20s = 41.333, before 41.5.
        self.assertEqual(snap["box"]["away"]["oreb"], 1)
        self.assertGreaterEqual(snap["away_score"], 110)


if __name__ == "__main__":
    unittest.main()
