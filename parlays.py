"""Pregame parlays for one slate.

Player legs come from ESPN game logs. A leg qualifies when the player cleared
the floor in at least 85% of the last 10 games and 85% of the season, with
stable minutes. Totals legs compare a simple team pace/scoring projection to
the FanDuel pregame total already stored on the slate. Probabilities are
UNCALIBRATED. The combined figure assumes independence and says so when two
legs share a game.

Player-prop prices are not fetched. The Odds API can return them, but only
one event at a time, at 1 credit per market per region. Four markets across a
12-game slate is about 48 credits a night, which does not fit next to the
live totals budget.
"""
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
TARGET_PARLAYS = 6
PLAYER_PARLAYS = 4
TOTAL_PARLAYS = 2
LEG_MIN = 4
LEG_MAX = 6
MAX_PLAYERS_PER_TEAM = 12

FLOORS = (
    ("points", "15+ points", 15),
    ("rebounds", "3+ rebounds", 3),
    ("threes", "1+ three", 1),
    ("steals", "1+ steal", 1),
)
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
            "threes": _made(val("threePointFieldGoalsMade-threePointFieldGoalsAttempted")) or 0,
            "steals": _num(val("steals")) or 0,
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
    for stat, label, threshold in FLOORS:
        season_rate, season_n = _rate(games, stat, threshold)
        recent_rate, recent_n = _rate(recent, stat, threshold)
        need = HIT_RATE_VS_STINGY if stingy and stat in ("points", "threes") else HIT_RATE_MIN
        if season_rate < need or recent_rate < need:
            continue
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
            "label": f"{name} {label}",
            "probability": probability,
            "probability_label": "UNCALIBRATED",
            "recent": f"{int(round(recent_rate * recent_n))}/{recent_n}",
            "season": f"{int(round(season_rate * season_n))}/{season_n}",
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
    """Greedy packs. At most two legs from the same player, prefer different games."""
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
            if len(chosen) >= LEG_MAX:
                rest.append(leg)
                continue
            if len(chosen) >= LEG_MIN and games.get(event, 0) >= 1 and any(games[k] == 0 for k in games):
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
        parlays.append({
            "legs": chosen,
            "combined_probability": combined,
            "probability_label": "UNCALIBRATED",
            "same_game": _shares_game(chosen),
            "result": None,
        })
        unused = rest
    return parlays


def build_parlays(player_legs, total_legs, slate):
    player_legs = sorted(player_legs, key=lambda leg: leg["probability"], reverse=True)
    parlays = []
    for kind, legs, count in (("player", player_legs, PLAYER_PARLAYS), ("total", total_legs, TOTAL_PARLAYS)):
        for index, parlay in enumerate(_pack(legs, count), start=1):
            parlay["kind"] = kind
            parlay["slate"] = slate
            parlay["id"] = f"{slate}-{kind}-{index}"
            parlays.append(parlay)
    return parlays[:TARGET_PARLAYS]


def render_parlays(parlays):
    """One message per kind. Returns a list of Telegram texts."""
    messages = []
    for kind, title, icon in (
        ("player", "Player Parlays", "🏀"),
        ("total", "Totals Parlays", "📊"),
    ):
        group = [parlay for parlay in parlays if parlay["kind"] == kind]
        if not group:
            continue
        lines = [f"{icon} <b>{title}</b> · UNCALIBRATED"]
        for index, parlay in enumerate(group, start=1):
            pct = _shown_pct(parlay["combined_probability"])
            lines.append(f"\n<b>{index}.</b> combined {pct}"
                         + (" · same-game legs, product ignores correlation" if parlay.get("same_game") else ""))
            for leg in parlay["legs"]:
                if leg["kind"] == "player":
                    detail = f"{leg['recent']} last {RECENT_GAMES}, {leg['season']} season"
                else:
                    detail = f"proj <b>{leg['projection']:.1f}</b>, edge {leg['edge']:+.1f}"
                lines.append(f"• {leg['label']} — {_shown_pct(leg['probability'])} ({detail})")
        lines.append("\nHit rates are historical floors, not prices. Independence is assumed.")
        messages.append("\n".join(lines))
    return messages


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
                "threes": _made(grab("3PT")) or 0,
                "steals": _num(grab("STL")) or 0,
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


def parlay_header(document):
    parlays = (document or {}).get("parlays") or []
    legs = [leg for parlay in parlays for leg in parlay.get("legs") or []]
    def bits(rows, key="result"):
        wins = sum(1 for row in rows if row.get(key) == "win")
        losses = sum(1 for row in rows if row.get(key) == "loss")
        pushes = sum(1 for row in rows if row.get(key) == "push")
        return recordbook.record_string(wins, losses, pushes)
    return f"Parlays ({bits(parlays)}) · Legs ({bits(legs)})"


def empty_parlays():
    return {"parlays": []}


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
                logs.extend(qualify_player(
                    athlete.get("displayName") or athlete.get("fullName"),
                    athlete.get("id"), team, opponent, event["id"], games, stingy))
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


def build_for_slate(slate, pregame, fetch=None):
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
    return build_parlays(player_legs, total_legs, slate.isoformat())
