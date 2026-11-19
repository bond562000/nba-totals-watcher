"""CourtSide Picks record.

The headline counts every posted OVER/UNDER once, graded on the official final
total including overtime. Integer lines push. PASS / NO PLAY and buyout alerts
are not results. A second posted call for the same game id is ignored; the
watcher only posts one call per game, and the first one is the grade.
"""
import html
from datetime import datetime, timedelta

# Verified against the ESPN scoreboard for 2026-11-17 (public, no key).
# BALLDONTLIE ids are the ones Richie logged. The id-to-game check is by tip
# time, because the games API requires a key:
#   21718126 Knicks @ Raptors tipped 7:30 PM ET; the post at 8:58 PM fits the
#            old Q3 window. Final 111-102 = 213.
#   21718127 The 8:00 PM game in the notes is Blazers @ Timberwolves, not the
#            other 8:00 PM game (Pistons @ Spurs). Post 9:46 PM. Final 105-106 = 211.
#   21718130 Post 12:04 AM ET Nov 18. Jazz @ Clippers tipped 10:30 PM, which
#            reaches the old Q3 window around then. Hornets @ Lakers tipped
#            11:00 PM, which is too late for a 12:04 AM post. Final 132-107 = 239.
SEED_CALLS = (
    {
        "game_id": "21718126",
        "slate": "2026-11-17",
        "away": "New York Knicks",
        "home": "Toronto Raptors",
        "away_nick": "Knicks",
        "home_nick": "Raptors",
        "side": "OVER",
        "line": 210.5,
        "posted_at": "2026-11-17T20:58:00-05:00",
        "source": "seed",
    },
    {
        "game_id": "21718127",
        "slate": "2026-11-17",
        "away": "Portland Trail Blazers",
        "home": "Minnesota Timberwolves",
        "away_nick": "Blazers",
        "home_nick": "Wolves",
        "side": "UNDER",
        "line": 207.5,
        "posted_at": "2026-11-17T21:46:00-05:00",
        "source": "seed",
    },
    {
        "game_id": "21718130",
        "slate": "2026-11-17",
        "away": "Utah Jazz",
        "home": "LA Clippers",
        "away_nick": "Jazz",
        "home_nick": "Clippers",
        "side": "OVER",
        "line": 242.5,
        "posted_at": "2026-11-18T00:04:00-05:00",
        "source": "seed",
    },
)

SEED_FINALS = {
    "21718126": (111, 102),
    "21718127": (105, 106),
    "21718130": (132, 107),
}


def empty_record():
    return {"calls": [], "confirmed": []}


def ensure_seeds(record):
    have = {c["game_id"] for c in record["calls"]}
    for seed in SEED_CALLS:
        if seed["game_id"] in have:
            continue
        record["calls"].append(dict(seed))
        have.add(seed["game_id"])


def add_call(record, call):
    """Insert one posted call. A repeat game id does not replace the first."""
    gid = str(call["game_id"])
    for existing in record["calls"]:
        if existing["game_id"] == gid:
            return False
    row = {
        "game_id": gid,
        "slate": call.get("slate"),
        "away": call.get("away") or "",
        "home": call.get("home") or "",
        "away_nick": call.get("away_nick") or _nick(call.get("away")),
        "home_nick": call.get("home_nick") or _nick(call.get("home")),
        "side": call["side"],
        "line": float(call["line"]),
        "posted_at": call.get("posted_at") or "",
        "source": call.get("source") or "watcher",
        "tier": "lean" if call.get("tier") == "lean" else "play",
    }
    if call["side"] not in ("OVER", "UNDER"):
        return False
    record["calls"].append(row)
    return True


def _nick(name):
    if not name:
        return ""
    return str(name).split()[-1]


def apply_final(call, away_score, home_score):
    """Grade once. A later call does not change a result that is already set."""
    if call.get("result") in ("win", "loss", "push"):
        return False
    total = int(away_score) + int(home_score)
    result = grade_call(call["side"], call["line"], total)
    if result is None:
        return False
    call["away_score"] = int(away_score)
    call["home_score"] = int(home_score)
    call["final_total"] = total
    call["result"] = result
    return True


def grade_call(side, line, final_total):
    """OVER wins above the line, UNDER wins below it, exact integer is a push.

    `final_total` is the official final, overtime included. PASS is not a result.
    """
    if side not in ("OVER", "UNDER") or line is None or final_total is None:
        return None
    line = float(line)
    final_total = float(final_total)
    integer = abs(line - round(line)) < 1e-9
    if integer and final_total == round(line):
        return "push"
    if side == "OVER":
        return "win" if final_total > line else "loss"
    return "win" if final_total < line else "loss"


def grade_ready(record, games_by_id, known_finals=None):
    """Grade calls that already have a final. Does not fetch."""
    known = dict(SEED_FINALS)
    if known_finals:
        known.update(known_finals)
    changed = False
    for bucket in (record["calls"], record.get("confirmed") or []):
        for call in bucket:
            if call.get("result") in ("win", "loss", "push"):
                continue
            scores = None
            gid = str(call.get("game_id"))
            game = games_by_id.get(gid)
            if game and game.get("state") == "final":
                scores = (game.get("away_score"), game.get("home_score"))
            elif gid in known:
                scores = known[gid]
            if scores and None not in scores:
                changed = apply_final(call, scores[0], scores[1]) or changed
    return changed


def tally(calls):
    wins = sum(1 for c in calls if c.get("result") == "win")
    losses = sum(1 for c in calls if c.get("result") == "loss")
    pushes = sum(1 for c in calls if c.get("result") == "push")
    return wins, losses, pushes


def record_string(wins, losses, pushes):
    if pushes:
        return f"{wins}-{losses}-{pushes}"
    return f"{wins}-{losses}"


def streak_label(calls):
    """Current run of wins or losses. Pushes are skipped, not a break."""
    ordered = sorted(calls, key=lambda c: c.get("posted_at") or "")
    last = None
    count = 0
    for call in ordered:
        result = call.get("result")
        if result not in ("win", "loss"):
            continue
        if last is None or result == last:
            count += 1
            last = result
        else:
            last = result
            count = 1
    if not last:
        return ""
    return ("W" if last == "win" else "L") + str(count)


def last_night_calls(calls, slate_date):
    prev = (slate_date - timedelta(days=1)).isoformat()
    rows = [c for c in calls if c.get("slate") == prev and c.get("result") in ("win", "loss", "push")]
    return sorted(rows, key=lambda c: c.get("posted_at") or "")


def _mark(result):
    return {"win": "✅", "loss": "❌", "push": "➖"}.get(result, "")


def _side_letter(side):
    return "O" if side == "OVER" else "U"


def format_header(record, slate_date, parlay_line=None):
    """Compact block placed above the slate title."""
    calls = record.get("calls") or []
    leans = [c for c in calls if c.get("tier") == "lean"]
    plays = [c for c in calls if c.get("tier") != "lean"]
    wins, losses, pushes = tally(plays)
    decided = wins + losses
    bits = [record_string(wins, losses, pushes)]
    if decided:
        bits.append(f"{int(round(100.0 * wins / decided))}%")
    streak = streak_label(plays)
    if streak:
        bits.append(streak)
    lw, ll, lp = tally(leans)
    headline = f"<b>CourtSide Picks ({' · '.join(bits)})</b> · Leans ({record_string(lw, ll, lp)})"
    if parlay_line:
        headline += f" · {parlay_line}"
    lines = [headline]
    last = last_night_calls(plays, slate_date)
    if last:
        parts = []
        for call in last:
            parts.append(
                f"{_mark(call['result'])} {html.escape(call.get('away_nick') or '')}/"
                f"{html.escape(call.get('home_nick') or '')} {_side_letter(call['side'])} "
                f"{_fmt_line(call['line'])}"
            )
        lines.append("Last night: " + " ".join(parts))
    return "\n".join(lines) + "\n"


def _fmt_line(line):
    line = float(line)
    if abs(line - round(line)) < 1e-9:
        return str(int(round(line)))
    return f"{line:g}"


def games_index(games):
    return {str(g.get("id")): g for g in games or []}


def call_from_posted(game_id, gs, game, slate):
    side = gs.get("posted_side")
    line = gs.get("posted_line")
    if side not in ("OVER", "UNDER") or line is None:
        return None
    posted_at = ""
    for decision in gs.get("decisions") or []:
        if decision.get("verdict") in ("OVER", "UNDER") and decision.get("at"):
            posted_at = decision["at"]
            break
    away = gs.get("away") or (game or {}).get("away") or ""
    home = gs.get("home") or (game or {}).get("home") or ""
    return {
        "game_id": str(game_id),
        "slate": slate,
        "away": away,
        "home": home,
        "away_nick": gs.get("away_nick") or (game or {}).get("away_nick") or _nick(away),
        "home_nick": gs.get("home_nick") or (game or {}).get("home_nick") or _nick(home),
        "side": side,
        "line": line,
        "posted_at": posted_at,
        "source": "watcher",
        "tier": "lean" if gs.get("posted_tier") == "lean" else "play",
    }


def ingest_state(record, state, games):
    """Pull posted OVER/UNDER calls out of one night's state. First call wins."""
    by_id = games_index(games)
    slate = state.get("slate")
    added = 0
    for gid, gs in (state.get("games") or {}).items():
        if not gs.get("posted"):
            continue
        call = call_from_posted(gid, gs, by_id.get(str(gid)), slate)
        if call and add_call(record, call):
            added += 1
    return added


def ingest_confirmed(record, rows):
    """Confirmed bets are stored apart from the headline."""
    have = {c.get("key") for c in record.setdefault("confirmed", [])}
    for row in rows or []:
        side = (row.get("confirmed_side") or "").upper()
        if side not in ("OVER", "UNDER"):
            continue
        try:
            line = float(row.get("confirmed_total"))
        except (TypeError, ValueError):
            continue
        key = f"{row.get('game_id')}:{row.get('checkpoint')}:{side}:{line}"
        if key in have:
            continue
        record["confirmed"].append({
            "key": key,
            "game_id": str(row.get("game_id") or ""),
            "slate": row.get("slate"),
            "away": row.get("away") or "",
            "home": row.get("home") or "",
            "away_nick": _nick(row.get("away")),
            "home_nick": _nick(row.get("home")),
            "side": side,
            "line": line,
            "posted_at": row.get("slate") or "",
            "checkpoint": row.get("checkpoint"),
            "source": "confirmed",
        })
        have.add(key)
