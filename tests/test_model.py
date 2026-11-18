"""Possession model, integer-line pushes, buyout threshold, and checkpoint grades."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model  # noqa: E402


class ModelTest(unittest.TestCase):
    def test_equal_remaining_possessions_identity(self):
        self.assertEqual(model.project_equal_remaining(185, 10, 1.10, 1.10), 207)
        self.assertEqual(model.project_from_remaining(185, 10, 1.10, 10, 1.10), 207)

    def test_offensive_rebound_extends_the_possession(self):
        without = model.estimate_possessions(fga=80, oreb=0, tov=12, fta=20)
        with_oreb = model.estimate_possessions(fga=80, oreb=5, tov=12, fta=20)
        self.assertAlmostEqual(without - with_oreb, 5)
        self.assertAlmostEqual(with_oreb, 80 - 5 + 12 + 0.44 * 20)

    def test_shrinkage_pulls_a_hot_night_toward_the_baseline(self):
        blended, weight = model.blend_rate(1.40, 10, model.LEAGUE_PPP, model.PPP_PRIOR_POSSESSIONS)
        self.assertLess(blended, 1.40)
        self.assertGreater(blended, model.LEAGUE_PPP)
        self.assertLess(weight, 0.5)

    def test_integer_line_push(self):
        self.assertEqual(model.grade_ou(210, 210, "OVER"), "push")
        self.assertEqual(model.grade_ou(210, 210, "UNDER"), "push")
        self.assertEqual(model.grade_ou(211, 210, "OVER"), "win")
        self.assertEqual(model.grade_ou(209, 210, "UNDER"), "win")
        self.assertEqual(model.grade_ou(211, 210.5, "OVER"), "win")
        self.assertEqual(model.grade_ou(210, 210.5, "OVER"), "loss")
        self.assertEqual(model.grade_ou(210, 210.5, "UNDER"), "win")
        p_over, p_under, p_push = model.ou_probabilities(210, 210, 8)
        self.assertGreater(p_push, 0)
        self.assertAlmostEqual(p_over + p_under + p_push, 1, places=6)
        p_over, p_under, p_push = model.ou_probabilities(210, 210.5, 8)
        self.assertEqual(p_push, 0)
        self.assertAlmostEqual(p_over + p_under, 1, places=6)

    def test_buyout_needs_a_real_swing(self):
        self.assertFalse(model.buyout_triggered("OVER", 228, 229.5, 0.20))
        self.assertFalse(model.buyout_triggered("OVER", 220, 229.5, 0.50))
        self.assertTrue(model.buyout_triggered("OVER", 220, 229.5, 0.20))
        self.assertTrue(model.buyout_triggered("UNDER", 230, 220, 0.10))

    def test_interval_is_labeled_uncalibrated_and_covers_the_center(self):
        lo, hi, sd = model.interval_80(207, 10, 10)
        self.assertLess(lo, 207)
        self.assertGreater(hi, 207)
        self.assertGreater(sd, 0)
        self.assertEqual(model.CALIBRATION, "UNCALIBRATED")

    def test_summary_excludes_pushes_from_the_hit_rate(self):
        records = [
            {"checkpoint": "q4_6:30", "final_total": 220, "proj": 215, "lo_80": 200, "hi_80": 230,
             "line_total": 210.5, "model_side": "OVER"},
            {"checkpoint": "q4_6:30", "final_total": 210, "proj": 212, "lo_80": 200, "hi_80": 230,
             "line_total": 210, "model_side": "OVER"},
            {"checkpoint": "q4_2:00", "final_total": 200, "proj": 220, "lo_80": 210, "hi_80": 230,
             "line_total": 218.5, "model_side": "OVER",
             "confirmed_total": 219, "confirmed_side": "UNDER"},
        ]
        summary = model.summarize_checkpoints(records)
        q4 = summary["q4_6:30"]
        self.assertEqual(q4["n"], 2)
        self.assertEqual(q4["ou_wins"], 1)
        self.assertEqual(q4["ou_pushes"], 1)
        self.assertEqual(q4["ou_hit_rate"], 1.0)
        self.assertEqual(q4["interval_coverage"], 1.0)
        late = summary["q4_2:00"]
        self.assertEqual(late["interval_coverage"], 0.0)
        self.assertEqual(late["confirmed_wins"], 1)
        self.assertEqual(late["interval_label"], "UNCALIBRATED")

    def test_fallback_when_box_is_missing(self):
        game = {"total": 200, "elapsed_min": 40, "remaining_min": 8, "margin": 4, "in_ot": False}
        pack = model.project_game(game, pregame_total=226.5)
        self.assertEqual(pack["model"], "pace_fallback")
        self.assertEqual(pack["settlement"], "full_game_including_ot")
        self.assertIn("team_box_fga_oreb_tov_fta", pack["missing_inputs"])
        self.assertGreater(pack["proj"], pack["proj_regulation"])

    def test_possession_path_uses_the_box(self):
        game = {
            "total": 214, "elapsed_min": 41.5, "remaining_min": 6.5, "margin": 6, "in_ot": False,
            "box": {
                "away": {"fga": 78, "fta": 22, "oreb": 10, "tov": 12, "pts": 110},
                "home": {"fga": 76, "fta": 20, "oreb": 9, "tov": 11, "pts": 104},
            },
        }
        pack = model.project_game(game, pregame_total=226.5)
        self.assertEqual(pack["model"], "possession_estimate")
        self.assertNotIn("team_box_fga_oreb_tov_fta", pack["missing_inputs"])
        self.assertIn("possession_arrow", pack["missing_inputs"])
        self.assertIn("current_lineup", pack["missing_inputs"])


if __name__ == "__main__":
    unittest.main()
