#!/usr/bin/env python3
"""Replay ESPN play-by-play to a few checkpoints and score the live projection.

This is a check, not a license to relabel the interval. Prints MAE and empirical
80% coverage. The watcher does not run this during a game.
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import espn_box  # noqa: E402
import model  # noqa: E402

MARKS = {
    "halftime": 24.0,
    "end_q3": 36.0,
    "q4_8:00": 40.0,
    "q4_6:30": 41.5,
    "q4_5:00": 43.0,
    "q4_2:00": 46.0,
}


def finals_on(day, cap):
    payload, code, _ = espn_box.fetch_json(f"{espn_box.SCOREBOARD}?dates={day.strftime('%Y%m%d')}")
    if payload is None:
        print(f"scoreboard {day} HTTP {code}", file=sys.stderr)
        return []
    out = []
    for event in espn_box.parse_scoreboard(payload):
        if event["state"] == "final" and event["id"]:
            out.append(event["id"])
        if len(out) >= cap:
            break
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    # 2026-11-17 is a completed slate in this environment. Walk backward.
    end = date(2026, 11, 17)
    ids = []
    for offset in range(days):
        if len(ids) >= cap:
            break
        ids.extend(eid for eid in finals_on(end - timedelta(days=offset), cap - len(ids)) if eid not in ids)
        time.sleep(0.2)
    buckets = {name: [] for name in MARKS}
    ot_points = []
    used = 0
    for eid in ids[:cap]:
        payload, code, _ = espn_box.fetch_json(f"{espn_box.SUMMARY}?event={eid}")
        time.sleep(0.25)
        if not payload or code != 200:
            print(f"summary {eid} HTTP {code}", file=sys.stderr)
            continue
        event = espn_box.parse_summary(payload)["event"]
        final = (event.get("away_score") or 0) + (event.get("home_score") or 0)
        if final <= 0:
            continue
        end_reg = espn_box.snapshot_from_plays(payload, 48)
        if end_reg["away_score"] or end_reg["home_score"]:
            reg_total = end_reg["away_score"] + end_reg["home_score"]
            if final > reg_total:
                ot_points.append(final - reg_total)
        used += 1
        for name, elapsed in MARKS.items():
            snap = espn_box.snapshot_from_plays(payload, elapsed)
            total = snap["away_score"] + snap["home_score"]
            if total <= 0 or snap["box"]["away"]["fga"] < 10:
                continue
            remaining = max(0.0, 48.0 - elapsed)
            game = {
                "total": total,
                "elapsed_min": elapsed,
                "remaining_min": remaining,
                "margin": abs(snap["away_score"] - snap["home_score"]),
                "in_ot": False,
                "box": snap["box"],
            }
            early = espn_box.snapshot_from_plays(payload, max(0.0, elapsed - 3.0))
            recent = {}
            for side in ("away", "home"):
                recent[side] = {
                    key: snap["box"][side][key] - early["box"][side][key]
                    for key in ("fga", "fta", "oreb", "tov", "pts")
                }
            game_recent = dict(game, recent_box=recent)
            pack = model.project_game(game, None)
            pack_r = model.project_game(game_recent, None)
            err = pack["proj"] - final
            buckets[name].append({
                "err": err,
                "abs": abs(err),
                "abs_recent": abs(pack_r["proj"] - final),
                "cover": model.covered(final, pack["lo_80"], pack["hi_80"]),
                "final": final,
                "proj": pack["proj"],
            })
        print(f"replayed {eid} final {final}", file=sys.stderr)
    print(f"games {used} ot_games {len(ot_points)} mean_ot_points "
          f"{(sum(ot_points)/len(ot_points)) if ot_points else 'n/a'}")
    for name, rows in buckets.items():
        n = len(rows)
        if not n:
            print(f"{name} n=0")
            continue
        mae = sum(r["abs"] for r in rows) / n
        mae_r = sum(r["abs_recent"] for r in rows) / n
        bias = sum(r["err"] for r in rows) / n
        cover = sum(1 for r in rows if r["cover"]) / n
        print(f"{name} n={n} MAE={mae:.2f} MAE_with_recent={mae_r:.2f} bias={bias:.2f} cover80={cover:.2f}")


if __name__ == "__main__":
    main()
