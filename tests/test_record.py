"""CourtSide record: seeds, pushes, overtime totals, and no double counting."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recordbook  # noqa: E402


def seeded():
    record = recordbook.empty_record()
    recordbook.ensure_seeds(record)
    recordbook.grade_ready(record, {})
    return record


class RecordTest(unittest.TestCase):
    def test_seeded_finals(self):
        record = seeded()
        by_id = {c["game_id"]: c for c in record["calls"]}
        knicks = by_id["21718126"]
        self.assertEqual(knicks["final_total"], 213)
        self.assertEqual((knicks["away_score"], knicks["home_score"]), (111, 102))
        self.assertEqual(knicks["result"], "win")
        self.assertEqual(knicks["side"], "OVER")
        self.assertEqual(knicks["line"], 210.5)
        wolves = by_id["21718127"]
        self.assertEqual(wolves["final_total"], 211)
        self.assertEqual(wolves["result"], "loss")
        jazz = by_id["21718130"]
        self.assertEqual(jazz["final_total"], 239)
        self.assertEqual((jazz["away_score"], jazz["home_score"]), (132, 107))
        self.assertEqual(jazz["result"], "loss")
        self.assertEqual(recordbook.tally(record["calls"]), (1, 2, 0))

    def test_header_is_compact(self):
        text = recordbook.format_header(seeded(), date(2026, 11, 18))
        self.assertTrue(text.startswith("<b>CourtSide Picks (1-2 · 33% · L2)</b>\n"))
        self.assertIn("Last night: ✅ Knicks/Raptors O 210.5 ❌ Blazers/Wolves U 207.5 ❌ Jazz/Clippers O 242.5", text)
        self.assertNotIn("1-2-0", text)

    def test_push_shows_the_third_number_and_is_not_a_win(self):
        self.assertEqual(recordbook.grade_call("UNDER", 211, 211), "push")
        self.assertEqual(recordbook.grade_call("OVER", 211, 211), "push")
        self.assertEqual(recordbook.grade_call("OVER", 210.5, 211), "win")
        self.assertEqual(recordbook.grade_call("UNDER", 207.5, 211), "loss")
        record = recordbook.empty_record()
        recordbook.add_call(record, {
            "game_id": "1", "slate": "2026-11-17", "away": "A", "home": "B",
            "away_nick": "A", "home_nick": "B", "side": "OVER", "line": 200,
            "posted_at": "2026-11-17T20:00:00-05:00",
        })
        recordbook.add_call(record, {
            "game_id": "2", "slate": "2026-11-17", "away": "C", "home": "D",
            "away_nick": "C", "home_nick": "D", "side": "UNDER", "line": 210,
            "posted_at": "2026-11-17T21:00:00-05:00",
        })
        recordbook.apply_final(record["calls"][0], 100, 101)  # 201 over 200
        recordbook.apply_final(record["calls"][1], 105, 105)  # 210 push
        self.assertEqual(recordbook.record_string(*recordbook.tally(record["calls"])), "1-0-1")
        self.assertEqual(recordbook.streak_label(record["calls"]), "W1")

    def test_overtime_total_is_the_official_final(self):
        # Regulation might have been short; the grade uses the full final, OT included.
        self.assertEqual(recordbook.grade_call("OVER", 230, 114 + 118), "win")
        self.assertEqual(recordbook.grade_call("UNDER", 230, 114 + 118), "loss")

    def test_pass_and_second_call_and_regrade_do_not_count(self):
        record = recordbook.empty_record()
        self.assertFalse(recordbook.add_call(record, {
            "game_id": "9", "side": "PASS / NO PLAY", "line": 200,
        }))
        self.assertTrue(recordbook.add_call(record, {
            "game_id": "9", "slate": "2026-11-18", "away": "Utah Jazz", "home": "LA Clippers",
            "side": "OVER", "line": 242.5, "posted_at": "2026-11-18T00:04:00-05:00",
        }))
        self.assertFalse(recordbook.add_call(record, {
            "game_id": "9", "slate": "2026-11-18", "away": "Utah Jazz", "home": "LA Clippers",
            "side": "UNDER", "line": 220, "posted_at": "2026-11-18T00:30:00-05:00",
        }))
        self.assertEqual(len(record["calls"]), 1)
        self.assertTrue(recordbook.apply_final(record["calls"][0], 132, 107))
        self.assertEqual(record["calls"][0]["result"], "loss")
        # A buyout, or a second look at the final, does not rewrite the grade.
        self.assertFalse(recordbook.apply_final(record["calls"][0], 140, 140))
        self.assertEqual(record["calls"][0]["final_total"], 239)
        self.assertEqual(record["calls"][0]["result"], "loss")

    def test_confirmed_bets_are_not_the_headline(self):
        record = seeded()
        recordbook.ingest_confirmed(record, [{
            "slate": "2026-11-17", "game_id": "21718126", "away": "New York Knicks",
            "home": "Toronto Raptors", "checkpoint": "q4_6:30",
            "confirmed_total": "210.5", "confirmed_side": "UNDER",
        }])
        recordbook.grade_ready(record, {})
        self.assertEqual(recordbook.tally(record["calls"]), (1, 2, 0))
        self.assertEqual(record["confirmed"][0]["result"], "loss")
        text = recordbook.format_header(record, date(2026, 11, 18))
        self.assertIn("(1-2 · 33% · L2)", text)
        self.assertNotIn("confirmed", text.lower())

    def test_ingest_from_watcher_state_once(self):
        record = recordbook.empty_record()
        state = {"slate": "2026-11-18", "games": {
            "55": {"posted": True, "posted_side": "OVER", "posted_line": 220.5,
                   "away": "Boston Celtics", "home": "Miami Heat",
                   "away_nick": "Celtics", "home_nick": "Heat",
                   "decisions": [{"verdict": "OVER", "at": "2026-11-18T23:00:00-05:00"}]},
            "56": {"posted": False, "posted_side": "UNDER", "posted_line": 200},
        }}
        games = [{"id": "55", "state": "final", "away_score": 110, "home_score": 112,
                  "away": "Boston Celtics", "home": "Miami Heat"}]
        self.assertEqual(recordbook.ingest_state(record, state, games), 1)
        self.assertEqual(recordbook.ingest_state(record, state, games), 0)
        recordbook.grade_ready(record, recordbook.games_index(games))
        self.assertEqual(record["calls"][0]["result"], "win")
        self.assertEqual(record["calls"][0]["final_total"], 222)


if __name__ == "__main__":
    unittest.main()
