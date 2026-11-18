#!/usr/bin/env python3
"""Append one confirmed total to NBA_STATE_DIR/confirmed-lines.csv.

Reads CONFIRMED_* environment variables. Does not call The Odds API.
"""
import csv
import os
import sys
from datetime import datetime

FIELDS = ["slate", "game_id", "away", "home", "checkpoint",
          "confirmed_total", "confirmed_side", "confirmed_price", "note"]
CHECKPOINTS = {"halftime", "end_q3", "q4_8:00", "q4_6:30", "q4_5:00", "q4_2:00"}


def need(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        print(f"missing {name}", file=sys.stderr)
        sys.exit(2)
    return value


def main():
    slate = need("CONFIRMED_SLATE")
    try:
        datetime.strptime(slate, "%Y-%m-%d")
    except ValueError:
        print("CONFIRMED_SLATE must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)
    checkpoint = need("CONFIRMED_CHECKPOINT")
    if checkpoint not in CHECKPOINTS:
        print(f"checkpoint must be one of {', '.join(sorted(CHECKPOINTS))}", file=sys.stderr)
        sys.exit(2)
    side = need("CONFIRMED_SIDE").upper()
    if side not in ("OVER", "UNDER"):
        print("CONFIRMED_SIDE must be OVER or UNDER", file=sys.stderr)
        sys.exit(2)
    try:
        total = float(need("CONFIRMED_TOTAL"))
    except ValueError:
        print("CONFIRMED_TOTAL must be a number", file=sys.stderr)
        sys.exit(2)
    state_dir = need("NBA_STATE_DIR")
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "confirmed-lines.csv")
    new_file = not os.path.exists(path)
    row = {
        "slate": slate,
        "game_id": need("CONFIRMED_GAME_ID"),
        "away": need("CONFIRMED_AWAY"),
        "home": need("CONFIRMED_HOME"),
        "checkpoint": checkpoint,
        "confirmed_total": total,
        "confirmed_side": side,
        "confirmed_price": (os.environ.get("CONFIRMED_PRICE") or "").strip(),
        "note": (os.environ.get("CONFIRMED_NOTE") or "").strip(),
    }
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    print(f"recorded {side} {total:g} for game {row['game_id']} at {checkpoint}")


if __name__ == "__main__":
    main()
