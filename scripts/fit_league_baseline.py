#!/usr/bin/env python3
"""Recompute the league PPP prior from an ESPN team-box CSV.

The live watcher does not download this file. Pass a local CSV from
https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_team_boxscores/team_box_YYYY.csv

Finals only: no quarter, no clock, no betting line. Do not treat the printed
PPP as a calibrated live forecast.
"""
import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import estimate_possessions  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("usage: fit_league_baseline.py team_box_YYYY.csv", file=sys.stderr)
        sys.exit(2)
    ppp, poss, games = [], [], {}
    with open(sys.argv[1], newline="") as f:
        for row in csv.DictReader(f):
            if row.get("season_type") != "2":
                continue
            try:
                est = estimate_possessions(
                    row["field_goals_attempted"], row["offensive_rebounds"],
                    row["turnovers"], row["free_throws_attempted"])
                pts = float(row["team_score"])
            except (KeyError, TypeError, ValueError):
                continue
            if est <= 20:
                continue
            ppp.append(pts / est)
            poss.append(est)
            games.setdefault(row["game_id"], []).append(pts)
    totals = [sum(v) for v in games.values() if len(v) == 2]
    print(f"team_rows {len(ppp)}")
    print(f"games {len(totals)}")
    print(f"poss_mean {statistics.mean(poss):.2f}")
    print(f"ppp_mean {statistics.mean(ppp):.4f}")
    print(f"ppp_sd {statistics.pstdev(ppp):.4f}")
    print(f"final_total_mean {statistics.mean(totals):.2f}")


if __name__ == "__main__":
    main()
