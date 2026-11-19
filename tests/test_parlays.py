"""Pregame parlay floors, combined probability, and grading. No network."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parlays  # noqa: E402


def _games(points, rebounds, assists, minutes=30):
    return [{
        "minutes": minutes, "points": points, "rebounds": rebounds, "assists": assists,
    } for _ in range(12)]


class ParlayTest(unittest.TestCase):
    def test_floor_needs_both_windows(self):
        steady = _games(22, 8, 6)
        legs = parlays.qualify_player("Jalen Brunson", "1", "Knicks", "Celtics", "g1", steady, False)
        labels = {leg["label"] for leg in legs}
        self.assertIn("Jalen Brunson 20+ points", labels)
        self.assertIn("Jalen Brunson 8+ rebounds", labels)
        self.assertIn("Jalen Brunson 6+ assists", labels)
        self.assertFalse(any("three" in leg["label"] or "steal" in leg["label"] for leg in legs))
        cold = _games(10, 8, 6)
        cold_legs = parlays.qualify_player("Bench", "2", "Knicks", "Celtics", "g1", cold, False)
        self.assertFalse(any("15+ points" in leg["label"] or "20+ points" in leg["label"] for leg in cold_legs))
        self.assertTrue(any("10+ points" in leg["label"] for leg in cold_legs))

    def test_stingy_defense_raises_the_points_bar(self):
        # 16/18 is 88.9%: clears 85%, not 90%. Recent 10 are all hits.
        games = _games(22, 8, 6) + _games(22, 8, 6)[:4]
        games += [{"minutes": 30, "points": 8, "rebounds": 8, "assists": 6} for _ in range(2)]
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
        player = parlays.qualify_player("A", "1", "Knicks", "Wolves", "a", _games(20, 6, 5), False)
        player += parlays.qualify_player("B", "2", "Knicks", "Wolves", "a", _games(18, 7, 4), False)
        player += parlays.qualify_player("C", "3", "Celtics", "Heat", "b", _games(24, 5, 5), False)
        player += parlays.qualify_player("D", "4", "Heat", "Celtics", "b", _games(16, 9, 3), False)
        built = parlays.build_parlays(player, legs, "2026-11-19", "early")
        self.assertTrue(built)
        self.assertTrue(built[0]["same_game"])
        text = "\n".join(parlays.render_parlays(built, "early"))
        self.assertIn("EARLY BOARD", text)
        self.assertIn("FanDuel may not have every prop open yet", text)
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
                "labels": ["PTS", "REB", "AST"],
                "athletes": [{"athlete": {"id": "9"}, "stats": ["18", "6", "4"]}],
            }]}]},
        })
        self.assertEqual(stats["9"]["assists"], 4)

    def test_header_counts_parlays_separately(self):
        document = {"parlays": [
            {"result": "loss", "legs": [{"result": "win"}, {"result": "loss"}]},
            {"result": None, "legs": [{"result": None}]},
        ]}
        self.assertEqual(parlays.parlay_header(document), "Parlays (0-1) · Legs (1-1)")

    def test_final_board_replaces_early_in_the_header(self):
        document = {"parlays": [
            {"slate": "2026-11-19", "board": "early", "result": "loss",
             "legs": [{"result": "loss"}]},
            {"slate": "2026-11-19", "board": "final", "result": "win",
             "legs": [{"result": "win"}]},
        ]}
        self.assertEqual(parlays.parlay_header(document), "Parlays (1-0) · Legs (1-0)")

    def test_close_games_skip_blowouts_and_price_a_safer_floor(self):
        games = [
            {"key": "a", "spread": 3, "total": 220, "event_id": "odds-a"},
            {"key": "b", "spread": 8, "total": 230, "event_id": "odds-b"},
            {"key": "c", "spread": 12, "total": 240, "event_id": "odds-c"},
            {"key": "d", "spread": 3, "total": 228, "event_id": "odds-d"},
        ]
        chosen, how = parlays.pick_prop_games(games)
        self.assertEqual(how, "closest spread")
        self.assertEqual([row["key"] for row in chosen], ["d", "a"])
        fallback, how = parlays.pick_prop_games([
            {"key": "a", "spread": None, "total": 210},
            {"key": "b", "spread": None, "total": 230},
        ])
        self.assertEqual(how, "highest total")
        self.assertEqual([row["key"] for row in fallback], ["b", "a"])
        leg = {
            "kind": "player", "player": "Jalen Brunson", "event_id": "espn",
            "match_key": "knicks@wolves", "stat": "points", "threshold": 15, "price": None,
        }
        parlays.attach_prop_prices([leg], {
            "knicks@wolves": {"jalen brunson": {"points": [
                {"point": 24.5, "price": -115},
                {"point": 14.5, "price": -450},
                {"point": 9.5, "price": -900},
            ]}},
        })
        self.assertEqual(leg["book_point"], 14.5)
        self.assertEqual(leg["price"], -450)
        self.assertEqual(parlays.combined_american([leg, {"price": -200}]), -120)

    def test_prop_budget_keeps_the_month_under_500(self):
        day = date(2026, 11, 19)
        self.assertTrue(parlays.props_allowed(412, 88, day))
        self.assertFalse(parlays.props_allowed(59, 441, day))
        self.assertFalse(parlays.props_allowed(60, 440, day))

    def test_final_board_names_the_legs_that_changed(self):
        early = [{"legs": [{"label": "A 10+ points"}, {"label": "B 3+ rebounds"}]}]
        final = [{"legs": [{"label": "B 3+ rebounds"}, {"label": "C 5+ assists"}]}]
        note = parlays.change_note(early, final)
        self.assertIn("Dropped: A 10+ points", note)
        self.assertIn("Added: C 5+ assists", note)
        text = "\n".join(parlays.render_parlays(
            [{"legs": [{"kind": "player", "label": "C 5+ assists", "probability": 0.9,
                        "recent": "9/10", "season": "18/20", "price": None}],
              "combined_probability": 0.9, "same_game": False, "combined_price": None}],
            "final", note))
        self.assertIn("FINAL BOARD", text)
        self.assertIn("Dropped: A 10+ points", text)

    def test_at_most_three_player_legs_from_one_game(self):
        legs = []
        for i in range(4):
            legs += parlays.qualify_player(f"P{i}", str(i), "Knicks", "Celtics", "g1", _games(22, 8, 6), False)
        legs += parlays.qualify_player("Q", "9", "Celtics", "Knicks", "g2", _games(22, 8, 6), False)
        built = parlays.build_parlays(legs, [], "2026-11-19", "final")
        self.assertTrue(built)
        self.assertLessEqual(len(built), parlays.FINAL_MAX)
        for parlay in built:
            from_g1 = [leg for leg in parlay["legs"] if leg["event_id"] == "g1"]
            self.assertLessEqual(len(from_g1), 3)


if __name__ == "__main__":
    unittest.main()
