"""Pregame parlays for one slate.

Player legs come from ESPN game logs. A leg qualifies when the player cleared
the floor in at least 85% of the last 10 games and 85% of the season, with
stable minutes. Totals legs compare a simple team pace/scoring projection to
the FanDuel pregame total already stored on the slate. Probabilities are
UNCALIBRATED. The combined figure assumes independence and says so when two
legs share a game.

The early board is ESPN history only. The final board buys FanDuel player
points, rebounds, and assists for the two closest games, on the three main
markets only. Alternate prop markets are not requested.
"""
import calendar
import os

import espn_box
import recordbook

HIT_RATE_MIN = 0.85
HIT_RATE_VS_STINGY = 0.90
RECENT_GAMES = 10
MIN_RECENT = 8
MIN_SEASON = 8
MIN_MINUTES = 24.0
MAX_MINUTE_SD = 10.0
STINGY_DEFENSE_N = 8
TOTAL_EDGE_MIN = 3.0
RECENT_FORM_WEIGHT = 0.4
EARLY_MAX = 6
FINAL_MAX = 7
TOTAL_PARLAYS = 2
LEG_MIN = 4
LEG_MAX = 6
MAX_PLAYER_LEGS_PER_GAME = 3
MAX_PLAYERS_PER_TEAM = 12
# Highest threshold that still clears the hit rate. Points, rebounds, assists only.
FLOOR_LADDERS = (
    ("points", (25, 20, 15, 10)),
    ("rebounds", (10, 8, 6, 5, 3)),
    ("assists", (8, 6, 5, 3)),
)
# The Odds API charges markets × regions. totals+spreads on one region is 2.
# Three prop markets × one region × two events is 6. Live totals stay at 1 per fetch.
PLAN_CREDITS = 500
LIVE_RESERVE_PER_NIGHT = 9
PREGAME_CREDITS = 2
PROP_CREDITS = 6
PROP_MIN_REMAINING = 60
SPREAD_BLOWOUT = 10
PROP_GAME_COUNT = 2
OUT_STATUSES = {"out", "doubtful", "suspension", "suspended", "injured reserve"}


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _minutes(text):
    if text is None or text == "":
        return None
    raw = str(text)
    if ":" in raw:
        mins, secs = raw.split(":", 1)
        try:
            return float(mins) + float(secs) / 60.0
        except ValueError:
            return None
    return _num(raw)


def _made(text):
    if not text or "-" not in str(text):
        return None
    made, _att = str(text).replace("–", "-").split("-", 1)
    return _num(made)


def player_games(gamelog):
    """Regular-season games, newest first. Each row is a stat dict."""
    names = gamelog.get("names") or []
    index = {name: i for i, name in enumerate(names)}
    rows = []
    for season in gamelog.get("seasonTypes") or []:
        if "regular" not in (season.get("displayName") or "").lower():
            continue
        for cat in season.get("categories") or []:
            for event in cat.get("events") or []:
                stats = event.get("stats") or []
                if not stats:
                    continue
                rows.append((str(event.get("eventId") or ""), stats, index))
    rows.sort(key=lambda row: row[0], reverse=True)
    parsed = []
    for event_id, stats, index in rows:
        def val(name, _stats=stats, _index=index):
            i = _index.get(name)
            if i is None or i >= len(_stats):
                return None
            return _stats[i]
        minutes = _minutes(val("minutes"))
        if minutes is None or minutes <= 0:
            continue
        parsed.append({
            "event_id": event_id,
            "minutes": minutes,
            "points": _num(val("points")) or 0,
            "rebounds": _num(val("totalRebounds")) or 0,
            "assists": _num(val("assists")) or 0,
        })
    return parsed


def _rate(games, stat, threshold):
    if not games:
        return 0.0, 0
    hits = sum(1 for game in games if game[stat] >= threshold)
    return hits / len(games), len(games)


def _stdev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def qualify_player(name, player_id, team, opponent, event_id, games, stingy):
    """Floor legs for one player. Empty when the sample or the minutes are thin."""
    if len(games) < MIN_SEASON:
        return []
    recent = games[:RECENT_GAMES]
    if len(recent) < MIN_RECENT:
        return []
    minutes = [game["minutes"] for game in recent]
    if sum(minutes) / len(minutes) < MIN_MINUTES:
        return []
    if _stdev(minutes) > MAX_MINUTE_SD:
        return []
    legs = []
    for stat, ladder in FLOOR_LADDERS:
        need = HIT_RATE_VS_STINGY if stingy and stat == "points" else HIT_RATE_MIN
        chosen = None
        for threshold in ladder:
            season_rate, season_n = _rate(games, stat, threshold)
            recent_rate, recent_n = _rate(recent, stat, threshold)
            if season_rate < need or recent_rate < need:
                continue
            chosen = (threshold, season_rate, season_n, recent_rate, recent_n)
            break
        if not chosen:
            continue
        threshold, season_rate, season_n, recent_rate, recent_n = chosen
        probability = min(season_rate, recent_rate)
        legs.append({
            "kind": "player",
            "player": name,
            "player_id": str(player_id),
            "team": team,
            "opponent": opponent,
            "event_id": str(event_id),
            "stat": stat,
            "threshold": threshold,
            "label": f"{name} {threshold}+ {stat}",
            "probability": probability,
            "probability_label": "UNCALIBRATED",
            "recent": f"{int(round(recent_rate * recent_n))}/{recent_n}",
            "season": f"{int(round(season_rate * season_n))}/{season_n}",
            "price": None,
            "result": None,
        })
    return legs


def is_out(status):
    return (status or "").strip().lower() in OUT_STATUSES


def project_total(home_season, away_season, home_recent, away_recent):
    """UNCALIBRATED total from each team's scoring and what the opponent allows.

    home_season is (points for, points against). Recent is the same pair over
    the last handful of games, or None.
    """
    def side(offense, defense):
        return (offense[0] + defense[1]) / 2.0

    season = side(home_season, away_season) + side(away_season, home_season)
    if not home_recent or not away_recent:
        return season
    recent = side(home_recent, away_recent) + side(away_recent, home_recent)
    return (1.0 - RECENT_FORM_WEIGHT) * season + RECENT_FORM_WEIGHT * recent


def totals_legs(games, pregame, projections):
    """One OVER or UNDER per game when the projection clears TOTAL_EDGE_MIN."""
    legs = []
    for game in games:
        if game.get("state") not in (None, "scheduled"):
            continue
        key = _game_key(game.get("away"), game.get("home"))
        line = pregame.get(key)
        if line is None:
            continue
        proj = projections.get(key)
        if proj is None:
            continue
        edge = proj - float(line)
        if abs(edge) < TOTAL_EDGE_MIN:
            continue
        side = "OVER" if edge > 0 else "UNDER"
        # A 3-point gap is the floor. Map it to a conservative probability
        # that stays under 70% so a totals leg is not treated like a lock.
        probability = min(0.68, 0.55 + (abs(edge) - TOTAL_EDGE_MIN) * 0.02)
        legs.append({
            "kind": "total",
            "event_id": str(game.get("espn_id") or game.get("id") or key),
            "away": game.get("away"),
            "home": game.get("home"),
            "away_nick": game.get("away_nick") or recordbook._nick(game.get("away")),
            "home_nick": game.get("home_nick") or recordbook._nick(game.get("home")),
            "side": side,
            "line": float(line),
            "projection": round(proj, 1),
            "edge": round(edge, 1),
            "label": f"{game.get('away_nick') or recordbook._nick(game.get('away'))}/"
                     f"{game.get('home_nick') or recordbook._nick(game.get('home'))} "
                     f"{side} {recordbook._fmt_line(line)}",
            "probability": probability,
            "probability_label": "UNCALIBRATED",
            "result": None,
        })
    legs.sort(key=lambda leg: abs(leg["edge"]), reverse=True)
    return legs


def _game_key(away, home):
    return f"{recordbook._nick(away).lower()}@{recordbook._nick(home).lower()}"


def _shares_game(legs):
    ids = [leg.get("event_id") for leg in legs if leg.get("event_id")]
    return len(ids) != len(set(ids))


def _pack(legs, size):
    """Greedy packs. At most two legs from one player and three player legs from one game."""
    unused = list(legs)
    parlays = []
    while len(unused) >= LEG_MIN and len(parlays) < size:
        chosen = []
        players = {}
        games = {}
        rest = []
        for leg in unused:
            player = leg.get("player_id") or leg.get("label")
            event = leg.get("event_id")
            if players.get(player, 0) >= 2:
                rest.append(leg)
                continue
            if leg.get("kind") == "player" and games.get(event, 0) >= MAX_PLAYER_LEGS_PER_GAME:
                rest.append(leg)
                continue
            if len(chosen) >= LEG_MAX:
                rest.append(leg)
                continue
            chosen.append(leg)
            players[player] = players.get(player, 0) + 1
            games[event] = games.get(event, 0) + 1
        if len(chosen) < LEG_MIN:
            break
        combined = 1.0
        for leg in chosen:
            combined *= leg["probability"]
        price = combined_american(chosen)
        parlays.append({
            "legs": chosen,
            "combined_probability": combined,
            "combined_price": price,
            "probability_label": "UNCALIBRATED",
            "same_game": _shares_game(chosen),
            "result": None,
        })
        unused = rest
    return parlays, unused


def build_parlays(player_legs, total_legs, slate, board="early"):
    """Up to 6 parlays on the early board and 7 on the final board."""
    limit = FINAL_MAX if board == "final" else EARLY_MAX
    total_slots = min(TOTAL_PARLAYS, limit if total_legs else 0)
    if total_legs and limit >= 5:
        total_slots = min(TOTAL_PARLAYS, 2)
    player_slots = max(0, limit - total_slots)
    priced = [leg for leg in player_legs if leg.get("price") is not None]
    plain = [leg for leg in player_legs if leg.get("price") is None]
    priced.sort(key=lambda leg: leg["probability"], reverse=True)
    plain.sort(key=lambda leg: leg["probability"], reverse=True)
    priced_rows, leftover = _pack(priced, min(2, player_slots))
    plain_rows, _rest = _pack(leftover + plain, player_slots - len(priced_rows))
    total_rows, _rest = _pack(total_legs, total_slots)
    rows = []
    for kind, group in (("player", priced_rows + plain_rows), ("total", total_rows)):
        for index, parlay in enumerate(group, start=1):
            parlay["kind"] = kind
            parlay["board"] = board
            parlay["slate"] = slate
            parlay["id"] = f"{slate}-{board}-{kind}-{index}"
            rows.append(parlay)
    return rows[:limit]


def _fmt_american(price):
    price = int(round(price))
    return f"+{price}" if price > 0 else str(price)


def american_to_decimal(price):
    price = float(price)
    if price >= 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def decimal_to_american(decimal):
    if decimal >= 2:
        return int(round((decimal - 1.0) * 100))
    if decimal <= 1:
        return None
    return int(round(-100.0 / (decimal - 1.0)))


def combined_american(legs):
    """American price of the product. None unless every leg has a price."""
    if not legs or any(leg.get("price") is None for leg in legs):
        return None
    decimal = 1.0
    for leg in legs:
        decimal *= american_to_decimal(leg["price"])
    return decimal_to_american(decimal)


def posted_change_note(changes):
    """Changed-leg markers only. A missing early board is a log line, not a post."""
    if not changes or changes.startswith("No early board"):
        return None
    return changes


def board_log_lines(board, rows, changes):
    """Diagnostics that stay in the Actions log and off the Telegram card."""
    lines = []
    if board == "early":
        lines.append("early board is ESPN history only; FanDuel may not have every prop open; 0 odds credits")
    if changes:
        lines.append(f"board diff: {changes}")
    priced = any(leg.get("price") is not None for parlay in rows or [] for leg in parlay.get("legs") or [])
    if board == "final" and rows and not priced:
        lines.append("no FanDuel floor at or below the history line; alternate prop markets were not bought")
    if any(parlay.get("same_game") for parlay in rows or []):
        lines.append("a parlay has same-game legs; the combined figure assumes independence")
    lines.append("hit rates are UNCALIBRATED; a combined price is the product of leg prices only when every leg has one")
    return lines


def render_parlays(parlays, board="early", changes=None):
    """One board message, split only when it would exceed Telegram's length."""
    if not parlays:
        return []
    title = "EARLY BOARD" if board == "early" else "FINAL BOARD"
    icon = "🏀" if board == "early" else "📊"
    lines = [f"{icon} <b>{title}</b>"]
    public_changes = posted_change_note(changes)
    if public_changes:
        lines.append(public_changes)
    for index, parlay in enumerate(parlays, start=1):
        pct = _shown_pct(parlay["combined_probability"])
        price = ""
        if parlay.get("combined_price") is not None:
            price = f" · FanDuel <b>{_fmt_american(parlay['combined_price'])}</b>"
        lines.append(f"\n<b>{index}.</b> combined {pct}{price}")
        for leg in parlay["legs"]:
            lines.append(f"• {leg['label']} — {_leg_detail(leg)}")
    text = "\n".join(lines)
    if len(text) <= 3500 or len(parlays) == 1:
        return [text]
    midpoint = max(1, len(parlays) // 2)
    return render_parlays(parlays[:midpoint], board, changes) + render_parlays(parlays[midpoint:], board, None)


def _leg_detail(leg):
    if leg.get("price") is not None:
        point = leg.get("book_point")
        book = f"FanDuel Over {point:g} ({_fmt_american(leg['price'])})"
        return f"{_shown_pct(leg['probability'])} · {book}"
    if leg["kind"] == "player":
        return f"{_shown_pct(leg['probability'])} ({leg['recent']} last {RECENT_GAMES}, {leg['season']} season)"
    return f"{_shown_pct(leg['probability'])} (proj <b>{leg['projection']:.1f}</b>, edge {leg['edge']:+.1f})"


def _shown_pct(probability):
    if probability >= 0.995:
        return ">99%"
    return f"{probability:.0%}"


def summary_is_final(payload):
    comps = ((payload or {}).get("header") or {}).get("competitions") or []
    if not comps:
        return False
    state = (((comps[0].get("status") or {}).get("type") or {}).get("state") or "")
    return state == "post"


def player_stats_from_summary(payload):
    """Map ESPN player id to points, rebounds, threes, and steals for one finished game."""
    out = {}
    for block in ((payload or {}).get("boxscore") or {}).get("players") or []:
        table = (block.get("statistics") or [{}])[0]
        labels = table.get("labels") or []
        for athlete in table.get("athletes") or []:
            pid = str((athlete.get("athlete") or {}).get("id") or "")
            row = athlete.get("stats") or []
            if not pid or athlete.get("didNotPlay"):
                continue

            def grab(label, _row=row, _labels=labels):
                if label not in _labels or _labels.index(label) >= len(_row):
                    return None
                return _row[_labels.index(label)]

            out[pid] = {
                "points": _num(grab("PTS")) or 0,
                "rebounds": _num(grab("REB")) or 0,
                "assists": _num(grab("AST")) or 0,
            }
    return out


def grade_leg(leg, actual):
    """actual is the stat (player) or the final total (game)."""
    if actual is None:
        return None
    if leg["kind"] == "player":
        return "win" if float(actual) >= float(leg["threshold"]) else "loss"
    return recordbook.grade_call(leg["side"], leg["line"], actual)


def grade_parlay(parlay):
    """Loss if any leg loses. Push if none lose and one pushes. Win if every leg wins."""
    results = [leg.get("result") for leg in parlay["legs"]]
    if any(result is None for result in results):
        return None
    if any(result == "loss" for result in results):
        return "loss"
    if any(result == "push" for result in results):
        return "push"
    return "win"


def active_parlays(document):
    """Final board replaces the early board for the record. Older cards have no board."""
    rows = (document or {}).get("parlays") or []
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("slate"), []).append(row)
    chosen = []
    for group in grouped.values():
        final = [row for row in group if row.get("board") == "final"]
        chosen.extend(final or group)
    return chosen


def parlay_header(document):
    parlays = active_parlays(document)
    legs = [leg for parlay in parlays for leg in parlay.get("legs") or []]
    def bits(rows, key="result"):
        wins = sum(1 for row in rows if row.get(key) == "win")
        losses = sum(1 for row in rows if row.get(key) == "loss")
        pushes = sum(1 for row in rows if row.get(key) == "push")
        return recordbook.record_string(wins, losses, pushes)
    return f"Parlays ({bits(parlays)}) · Legs ({bits(legs)})"


def empty_parlays():
    return {"parlays": []}


def _person(name):
    text = (name or "").lower().replace(".", "").replace("'", "")
    for suffix in (" iii", " ii", " iv", " jr", " sr"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return " ".join(text.split())


def pick_prop_games(meta):
    """Two closest games by absolute spread. |spread| >= 10 is out.

    meta items need key, spread, total. When no game has a spread, the two
    highest totals are the fallback. A blowout is not filled back in.
    """
    rows = list(meta or [])
    known = [row for row in rows if row.get("spread") is not None]
    if not known:
        ranked = sorted(rows, key=lambda row: -(row.get("total") or 0))
        return ranked[:PROP_GAME_COUNT], "highest total"
    eligible = [row for row in known if abs(row["spread"]) < SPREAD_BLOWOUT]
    eligible.sort(key=lambda row: (abs(row["spread"]), -(row.get("total") or 0)))
    return eligible[:PROP_GAME_COUNT], "closest spread"


def props_allowed(credits_remaining, credits_used, day, live_spent_today=0, pregame_spent_today=0):
    """True when today's 6 prop credits still leave 9 live and 2 pregame for the month."""
    if credits_remaining is None or credits_remaining < PROP_MIN_REMAINING:
        return False
    if credits_used is None:
        credits_used = PLAN_CREDITS - int(credits_remaining)
    days_left = calendar.monthrange(day.year, day.month)[1] - day.day + 1
    tonight_live = max(0, LIVE_RESERVE_PER_NIGHT - live_spent_today)
    pregame_left = max(0, PREGAME_CREDITS - pregame_spent_today)
    future = max(0, days_left - 1) * (LIVE_RESERVE_PER_NIGHT + PREGAME_CREDITS)
    projected = credits_used + PROP_CREDITS + tonight_live + pregame_left + future
    return projected <= PLAN_CREDITS


def parse_prop_payload(payload):
    """FanDuel Overs from player_points, player_rebounds, and player_assists."""
    stat_of = {
        "player_points": "points",
        "player_rebounds": "rebounds",
        "player_assists": "assists",
    }
    found = {}
    for book in (payload or {}).get("bookmakers") or []:
        if book.get("key") not in (None, "fanduel"):
            continue
        for market in book.get("markets") or []:
            stat = stat_of.get(market.get("key"))
            if not stat:
                continue
            for outcome in market.get("outcomes") or []:
                if outcome.get("name") != "Over":
                    continue
                point = _num(outcome.get("point"))
                price = _num(outcome.get("price"))
                if point is None or price is None:
                    continue
                who = _person(outcome.get("description") or outcome.get("player") or "")
                if not who:
                    continue
                found.setdefault(who, {}).setdefault(stat, []).append(
                    {"point": point, "price": int(price)})
    return found


def attach_prop_prices(legs, quotes_by_event):
    """Price a historical floor when FanDuel has an Over at or below that floor.

    The main market usually posts one number near a coin flip. A 24.5 does not
    replace a 15+ floor. Alternate markets are a separate purchase and are not
    in `quotes_by_event` unless the response happened to include them.
    """
    for leg in legs:
        if leg.get("kind") != "player":
            continue
        bucket = (quotes_by_event or {}).get(str(leg.get("event_id")))
        if not bucket:
            bucket = (quotes_by_event or {}).get(leg.get("match_key")) or {}
        offers = bucket.get(_person(leg.get("player"))) or {}
        choices = offers.get(leg.get("stat")) or []
        ceiling = float(leg["threshold"]) - 0.5
        usable = [offer for offer in choices if offer["point"] <= ceiling + 1e-9]
        if not usable:
            continue
        best = max(usable, key=lambda offer: (offer["point"], -abs(offer["price"])))
        leg["price"] = best["price"]
        leg["book_point"] = best["point"]
        # The book's Over is the leg we can grade and price. Keep the history rate.
        leg["threshold"] = best["point"] + 0.5


def board_labels(parlays):
    return {leg.get("label") for parlay in parlays or [] for leg in parlay.get("legs") or []}


def change_note(early, final):
    if not early:
        return "No early board was posted."
    dropped = sorted(board_labels(early) - board_labels(final))
    added = sorted(board_labels(final) - board_labels(early))
    if not dropped and not added:
        return "Same legs as the early board."
    parts = ["Changed vs the early board."]
    if dropped:
        parts.append("Dropped: " + "; ".join(dropped[:12]) + ("…" if len(dropped) > 12 else ""))
    if added:
        parts.append("Added: " + "; ".join(added[:12]) + ("…" if len(added) > 12 else ""))
    return " ".join(parts)


# --- ESPN collection (used by the live run and --parlays, not by unit tests) ---

def _roster(abbr, fetch):
    payload, code = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{abbr.lower()}/roster")
    if not payload or code != 200:
        return []
    return payload.get("athletes") or []


def _gamelog(player_id, fetch):
    payload, code = fetch(
        "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/"
        f"{player_id}/gamelog")
    if not payload or code != 200:
        return []
    return player_games(payload)


def _standings(fetch):
    payload, code = fetch("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings")
    out = {}
    if not payload or code != 200:
        return out
    for child in payload.get("children") or []:
        for entry in ((child.get("standings") or {}).get("entries") or []):
            name = (entry.get("team") or {}).get("displayName")
            stats = {row.get("name"): row.get("value") for row in entry.get("stats") or []}
            if name:
                out[name] = {
                    "for": stats.get("avgPointsFor"),
                    "against": stats.get("avgPointsAgainst"),
                }
    return out


def _injuries(event_id, fetch):
    payload, code = fetch(f"{espn_box.SUMMARY}?event={event_id}")
    out = {}
    if not payload or code != 200:
        return out
    for team in payload.get("injuries") or []:
        for item in team.get("injuries") or []:
            athlete = item.get("athlete") or {}
            out[str(athlete.get("id"))] = item.get("status") or ""
    return out


def _stingy(name, table):
    rated = [(team, row.get("against")) for team, row in table.items() if row.get("against") is not None]
    rated.sort(key=lambda item: item[1])
    worst = {team for team, _value in rated[:STINGY_DEFENSE_N]}
    return name in worst


def collect_player_legs(events, fetch):
    """events are parse_event dicts with abbreviations. One gamelog per active player, capped."""
    logs = []
    defense = _standings(fetch)
    for event in events:
        if event.get("state") not in (None, "scheduled"):
            continue
        injured = _injuries(event["id"], fetch)
        for side, opp_side, abbr_key in (
            ("away", "home", "away_abbr"),
            ("home", "away", "home_abbr"),
        ):
            abbr = event.get(abbr_key)
            if not abbr:
                continue
            team = event.get(side)
            opponent = event.get(opp_side)
            stingy = _stingy(opponent, defense)
            fetched = 0
            cap = int(os.environ.get("NBA_PARLAY_PLAYER_CAP") or MAX_PLAYERS_PER_TEAM)
            for athlete in _roster(abbr, fetch):
                if fetched >= cap:
                    break
                status = injured.get(str(athlete.get("id"))) or ""
                if is_out(status):
                    continue
                if (athlete.get("status") or {}).get("type") == "inactive":
                    continue
                fetched += 1
                games = _gamelog(athlete.get("id"), fetch)
                fresh = qualify_player(
                    athlete.get("displayName") or athlete.get("fullName"),
                    athlete.get("id"), team, opponent, event["id"], games, stingy)
                match_key = _game_key(event.get("away"), event.get("home"))
                for leg in fresh:
                    leg["match_key"] = match_key
                logs.extend(fresh)
    return logs


def team_form(table):
    """Map display name -> (ppg, opp ppg) from season standings. Recent form is not separate."""
    out = {}
    for name, row in table.items():
        if row.get("for") is None or row.get("against") is None:
            continue
        out[name] = (float(row["for"]), float(row["against"]))
    return out


def _recent_form(abbr, team_name, fetch, n=5):
    """Mean points scored and allowed over the last n finals. None if the sample is short."""
    payload, code = fetch(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{abbr.lower()}/schedule")
    if not payload or code != 200:
        return None
    rows = []
    for event in payload.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        if (((comp.get("status") or {}).get("type") or {}).get("state")) != "post":
            continue
        scored = allowed = None
        for team in comp.get("competitors") or []:
            raw = team.get("score")
            value = raw.get("value") if isinstance(raw, dict) else raw
            value = _num(value)
            if value is None:
                continue
            info = team.get("team") or {}
            if (info.get("abbreviation") or "").lower() == abbr.lower() or info.get("displayName") == team_name:
                scored = value
            else:
                allowed = value
        if scored is not None and allowed is not None:
            rows.append((event.get("date") or "", scored, allowed))
    rows.sort()
    last = rows[-n:]
    if len(last) < 3:
        return None
    return (sum(row[1] for row in last) / len(last), sum(row[2] for row in last) / len(last))


def projections_for(events, form, recent=None):
    recent = recent or {}
    out = {}
    for event in events:
        if event.get("state") not in (None, "scheduled"):
            continue
        home = form.get(event.get("home"))
        away = form.get(event.get("away"))
        if not home or not away:
            continue
        key = _game_key(event.get("away"), event.get("home"))
        out[key] = project_total(home, away, recent.get(event.get("home")), recent.get(event.get("away")))
    return out


def slate_events(slate, fetch):
    payload, code = fetch(f"{espn_box.SCOREBOARD}?dates={slate.strftime('%Y%m%d')}")
    if not payload or code != 200:
        return None
    events = espn_box.parse_scoreboard(payload)
    # Abbreviations are not on the parsed event. Read them from the raw board.
    raw = {}
    for item in payload.get("events") or []:
        comp = (item.get("competitions") or [{}])[0]
        sides = {}
        for team in comp.get("competitors") or []:
            sides[team.get("homeAway")] = (team.get("team") or {}).get("abbreviation")
        raw[str(item.get("id"))] = sides
    for event in events:
        sides = raw.get(event["id"]) or {}
        event["away_abbr"] = sides.get("away")
        event["home_abbr"] = sides.get("home")
    return events


def build_for_slate(slate, pregame, board="early", prop_quotes=None, fetch=None):
    """Network entry used by --parlays and the noon run. fetch returns (payload, code)."""
    fetch = fetch or (lambda url: espn_box.fetch_json(url)[:2])
    events = slate_events(slate, fetch)
    if events is None:
        return None
    scheduled = [event for event in events if event.get("state") == "scheduled"]
    defense = _standings(fetch)
    form = team_form(defense)
    recent = {}
    for event in scheduled:
        for side, abbr_key in (("away", "away_abbr"), ("home", "home_abbr")):
            name = event.get(side)
            abbr = event.get(abbr_key)
            if name and abbr and name not in recent:
                recent[name] = _recent_form(abbr, name, fetch)
    projections = projections_for(scheduled, form, recent)
    # Reuse the standings already fetched inside collect by passing events through.
    player_legs = collect_player_legs(scheduled, fetch) if os.environ.get("NBA_SKIP_PLAYER_FETCH") != "1" else []
    # collect_player_legs fetches standings again. Acceptable once per slate.
    games = [{
        "away": event.get("away"), "home": event.get("home"),
        "away_nick": recordbook._nick(event.get("away")),
        "home_nick": recordbook._nick(event.get("home")),
        "state": "scheduled", "espn_id": event.get("id"), "id": event.get("id"),
    } for event in scheduled]
    total_legs = totals_legs(games, pregame or {}, projections)
    if prop_quotes:
        attach_prop_prices(player_legs, prop_quotes)
    return build_parlays(player_legs, total_legs, slate.isoformat(), board)
