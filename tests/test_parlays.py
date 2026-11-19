"""Pregame parlay floors, combined probability, and grading. No network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parlays  # noqa: E402


def _games(points, rebounds, threes, steals, minutes=30):
    return [{
        "minutes": minutes, "points": points, "rebounds": rebounds,
        "threes": threes, "steals": steals,
    } for _ in range(12)]


class ParlayTest(unittest.TestCase):
    def test_floor_needs_both_windows(self):
        steady = _games(22, 8, 3, 1)
        legs = parlays.qualify_player("Jalen Brunson", "1", "Knicks", "Celtics", "g1", steady, False)
        labels = {leg["label"] for leg in legs}
        self.assertIn("Jalen Brunson 15+ points", labels)
        self.assertIn("Jalen Brunson 3+ rebounds", labels)
        self.assertIn("Jalen Brunson 1+ three", labels)
        self.assertIn("Jalen Brunson 1+ steal", labels)
        cold = _games(10, 8, 3, 1)
        cold_legs = parlays.qualify_player("Bench", "2", "Knicks", "Celtics", "g1", cold, False)
        self.assertFalse(any("15+ points" in leg["label"] for leg in cold_legs))

    def test_stingy_defense_raises_the_points_bar(self):
        # 16/18 is 88.9%: clears 85%, not 90%. Recent 10 are all hits.
        games = _games(22, 8, 2, 1) + _games(22, 8, 2, 1)[:4]
        games += [{"minutes": 30, "points": 8, "rebounds": 8, "threes": 2, "steals": 1} for _ in range(2)]
        loose = parlays.qualify_player("Scorer", "3", "Knicks", "Celtics", "g1", games, False)
        stingy = parlays.qualify_player("Scorer", "3", "Knicks", "Celtics", "g1", games, True)
        self.assertTrue(any(leg["stat"] == "points" for leg in loose))
        self.assertFalse(any(leg["stat"] == "points" for leg in stingy))

    def test_totals_need_a_real_gap_and_a_parlay_notes_same_game(self):
        games = [
            {"away": "New York Knicks", "home": "Minnesota Timberwolves",
             "away_nick": "Knicks", "home_nick": "Wolves", "state": "scheduled", "espn_id": "a"},
            {"away": "Boston Celtics", "home": "Miami Heat",
             "away_nick": "Celtics", "home_nick": "Heat", "state": "scheduled", "espn_id": "b"},
        ]
        pregame = {"knicks@timberwolves": 220.0, "celtics@heat": 226.0}
        projections = {"knicks@timberwolves": 226.5, "celtics@heat": 227.0}
        legs = parlays.totals_legs(games, pregame, projections)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["side"], "OVER")
        self.assertEqual(legs[0]["probability_label"], "UNCALIBRATED")
        player = parlays.qualify_player("A", "1", "Knicks", "Wolves", "a", _games(20, 6, 2, 2), False)
        player += parlays.qualify_player("B", "2", "Knicks", "Wolves", "a", _games(18, 7, 2, 1), False)
        player += parlays.qualify_player("C", "3", "Celtics", "Heat", "b", _games(24, 5, 3, 1), False)
        player += parlays.qualify_player("D", "4", "Heat", "Celtics", "b", _games(16, 9, 1, 1), False)
        built = parlays.build_parlays(player, legs, "2026-11-19")
        self.assertTrue(built)
        self.assertTrue(built[0]["same_game"])
        text = "\n".join(parlays.render_parlays(built))
        self.assertIn("UNCALIBRATED", text)
        self.assertIn(">99%", text)
        self.assertIn("same-game legs", text)
        self.assertNotIn("apiKey", text)

    def test_grade_leg_and_parlay_once(self):
        leg = {"kind": "player", "stat": "points", "threshold": 15, "result": None}
        self.assertEqual(parlays.grade_leg(leg, 15), "win")
        self.assertEqual(parlays.grade_leg(leg, 14), "loss")
        total = {"kind": "total", "side": "OVER", "line": 220, "result": None}
        self.assertEqual(parlays.grade_leg(total, 220), "push")
        self.assertEqual(parlays.grade_leg(total, 221), "win")
        parlay = {"legs": [
            {"result": "win"}, {"result": "win"}, {"result": "loss"}, {"result": "win"},
        ]}
        self.assertEqual(parlays.grade_parlay(parlay), "loss")
        parlay["legs"][2]["result"] = "push"
        self.assertEqual(parlays.grade_parlay(parlay), "push")
        parlay["legs"][2]["result"] = "win"
        self.assertEqual(parlays.grade_parlay(parlay), "win")
        self.assertIsNone(parlays.grade_parlay({"legs": [{"result": None}]}))

    def test_recent_form_moves_the_projection(self):
        flat = parlays.project_total((110, 110), (110, 110), None, None)
        hot = parlays.project_total((110, 110), (110, 110), (120, 120), (120, 120))
        self.assertEqual(flat, 220)
        self.assertGreater(hot, flat)

    def test_final_box_only(self):
        self.assertFalse(parlays.summary_is_final({"header": {"competitions": [{"status": {"type": {"state": "in"}}}]}}))
        self.assertTrue(parlays.summary_is_final({"header": {"competitions": [{"status": {"type": {"state": "post"}}}]}}))
        stats = parlays.player_stats_from_summary({
            "boxscore": {"players": [{"statistics": [{
                "labels": ["PTS", "REB", "3PT", "STL"],
                "athletes": [{"athlete": {"id": "9"}, "stats": ["18", "6", "2-5", "1"]}],
            }]}]},
        })
        self.assertEqual(stats["9"]["threes"], 2)

    def test_header_counts_parlays_separately(self):
        document = {"parlays": [
            {"result": "loss", "legs": [{"result": "win"}, {"result": "loss"}]},
            {"result": None, "legs": [{"result": None}]},
        ]}
        self.assertEqual(parlays.parlay_header(document), "Parlays (0-1) · Legs (1-1)")


if __name__ == "__main__":
    unittest.main()
