"""ESPN public NBA scoreboard and game summary, with no API key.

ESPN's site API is unofficial. Requests stay small: one scoreboard per slate
date per poll, then one summary per live game, reused for about 50 seconds.
A 429 or a network error backs off and the watcher keeps the pace fallback.

A summary is applied only when its score matches BALLDONTLIE and, for a live
game, the period and clock agree within 90 seconds. Anything else is logged
and ignored.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import timedelta

import model

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
USER_AGENT = "nba-totals-watcher/1.0"
TIMEOUT_S = 12
CACHE_TTL_S = 50
CLOCK_TOLERANCE_S = 90
_CACHE = {}


def team_key(name):
    if not name:
        return ""
    return str(name).strip().split()[-1].lower()


def parse_clock(text):
    """Seconds left from an ESPN clock: '6:30', '0:35.4', or '35.4'."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or raw in {"-", "—"}:
        return None
    if ":" in raw:
        mins, secs = raw.split(":", 1)
        try:
            return int(mins) * 60 + float(secs)
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def _status_state(status):
    kind = (status or {}).get("type") or {}
    desc = " ".join(filter(None, [kind.get("description"), kind.get("detail"), kind.get("shortDetail")])).lower()
    if kind.get("completed") or kind.get("state") == "post" or "final" in desc:
        return "final", desc
    if "halftime" in desc:
        return "halftime", desc
    if kind.get("state") == "in" or "quarter" in desc or "end of" in desc:
        return "in_progress", desc
    return "scheduled", desc


def _int_score(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def parse_event(event):
    """One scoreboard event, or the header competition of a summary."""
    comp = (event.get("competitions") or [event])[0]
    status = comp.get("status") or event.get("status") or {}
    state, desc = _status_state(status)
    sides = {}
    for comp_team in comp.get("competitors") or []:
        side = comp_team.get("homeAway")
        team = comp_team.get("team") or {}
        lines = []
        for row in comp_team.get("linescores") or []:
            if isinstance(row, dict):
                raw = row.get("value")
                if raw is None:
                    raw = row.get("displayValue")
            else:
                raw = row
            val = _int_score(raw)
            if val is not None:
                lines.append(val)
        sides[side] = {
            "name": team.get("displayName") or team.get("name") or team.get("shortDisplayName"),
            "abbr": team.get("abbreviation"),
            "id": str(team.get("id") or ""),
            "score": _int_score(comp_team.get("score")),
            "linescores": lines,
            "possession": bool(comp_team.get("possession")),
        }
    away, home = sides.get("away") or {}, sides.get("home") or {}
    poss = None
    if away.get("possession") and not home.get("possession"):
        poss = "away"
    elif home.get("possession") and not away.get("possession"):
        poss = "home"
    return {
        "id": str(event.get("id") or comp.get("id") or ""),
        "away": away.get("name"),
        "home": home.get("name"),
        "away_id": away.get("id"),
        "home_id": home.get("id"),
        "away_score": away.get("score"),
        "home_score": home.get("score"),
        "period": status.get("period") or 0,
        "clock_secs": parse_clock(status.get("displayClock")),
        "state": state,
        "detail": desc,
        "linescores": {"away": away.get("linescores") or [], "home": home.get("linescores") or []},
        "possession_side": poss,
    }


def parse_scoreboard(payload):
    return [parse_event(ev) for ev in (payload or {}).get("events") or []]


def _stat_map(statistics):
    out = {}
    for row in statistics or []:
        name = row.get("name")
        if name:
            out[name] = row.get("displayValue")
    return out


def _made_attempted(text):
    if not text or "-" not in str(text):
        return None, None
    made, att = str(text).replace("–", "-").split("-", 1)
    try:
        return float(made), float(att)
    except ValueError:
        return None, None


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def team_box_from_stats(statistics, points):
    stats = _stat_map(statistics)
    _fgm, fga = _made_attempted(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
    _ftm, fta = _made_attempted(stats.get("freeThrowsMade-freeThrowsAttempted"))
    oreb = _num(stats.get("offensiveRebounds"))
    # Team turnovers end a possession, so prefer the combined total.
    tov = _num(stats.get("totalTurnovers"))
    if tov is None:
        tov = _num(stats.get("turnovers"))
    fouls = _num(stats.get("fouls"))
    if None in (fga, fta, oreb, tov):
        return None
    return {"fga": fga, "fta": fta, "oreb": oreb, "tov": tov, "pts": points, "fouls": fouls}


def _play_type(play):
    return ((play.get("type") or {}).get("text") or "").strip()


def _play_clock(play):
    clock = play.get("clock") or {}
    return parse_clock(clock.get("displayValue") if isinstance(clock, dict) else clock)


def _play_period(play):
    period = play.get("period") or {}
    if isinstance(period, dict):
        try:
            return int(period.get("number") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(period or 0)
    except (TypeError, ValueError):
        return 0


def _elapsed_sec(period, clock_secs):
    if not period or clock_secs is None or period > 4:
        return None
    return (period - 1) * 12 * 60 + (12 * 60 - float(clock_secs))


def _is_team_rebound(play):
    return "team rebound" in (play.get("text") or "").lower()


def _accumulate(plays, side_of, end_elapsed=None):
    """Box estimate from plays up to an elapsed-time cutoff. Player OREB only."""
    box = {
        "away": {"fga": 0, "fta": 0, "oreb": 0, "tov": 0, "pts": 0, "fouls_period": 0},
        "home": {"fga": 0, "fta": 0, "oreb": 0, "tov": 0, "pts": 0, "fouls_period": 0},
    }
    last_period = 0
    last = None
    for play in plays:
        period = _play_period(play)
        if period > 4:
            break
        clock = _play_clock(play)
        elapsed = _elapsed_sec(period, clock)
        if end_elapsed is not None and elapsed is not None and elapsed > end_elapsed:
            break
        if period != last_period and period:
            for side in box.values():
                side["fouls_period"] = 0
            last_period = period
        side = side_of((play.get("team") or {}).get("id"))
        typ = _play_type(play)
        low = typ.lower()
        if side:
            if "free throw" in low:
                box[side]["fta"] += 1
            elif "turnover" in low:
                box[side]["tov"] += 1
            elif typ == "Offensive Rebound" and not _is_team_rebound(play):
                box[side]["oreb"] += 1
            elif play.get("shootingPlay"):
                box[side]["fga"] += 1
            if "foul" in low and "technical" not in low:
                box[side]["fouls_period"] += 1
        away_score = _int_score(play.get("awayScore"))
        home_score = _int_score(play.get("homeScore"))
        if away_score is not None:
            box["away"]["pts"] = away_score
        if home_score is not None:
            box["home"]["pts"] = home_score
        last = play
    return box, last


def _recent_box(plays, side_of, now_period, now_clock):
    now_elapsed = _elapsed_sec(now_period, now_clock)
    if now_elapsed is None:
        return None
    start = max(0, now_elapsed - model.RECENT_WINDOW_SEC)
    before, _ = _accumulate(plays, side_of, end_elapsed=start)
    current, _ = _accumulate(plays, side_of, end_elapsed=now_elapsed + 0.01)
    out = {}
    for side in ("away", "home"):
        out[side] = {
            "fga": current[side]["fga"] - before[side]["fga"],
            "fta": current[side]["fta"] - before[side]["fta"],
            "oreb": current[side]["oreb"] - before[side]["oreb"],
            "tov": current[side]["tov"] - before[side]["tov"],
            "pts": current[side]["pts"] - before[side]["pts"],
        }
    return out


def _lineup(block):
    stats = (block.get("statistics") or [{}])[0]
    names, starters, active = [], [], []
    for athlete in stats.get("athletes") or []:
        person = athlete.get("athlete") or {}
        name = person.get("shortName") or person.get("displayName")
        if not name or athlete.get("didNotPlay"):
            continue
        names.append(name)
        if athlete.get("starter"):
            starters.append(name)
        if athlete.get("active"):
            active.append(name)
    return starters, active


def parse_summary(payload):
    header = (payload or {}).get("header") or {}
    event = parse_event({"id": header.get("id"), "competitions": header.get("competitions") or []})
    comp = (header.get("competitions") or [{}])[0]
    if not event["id"]:
        event["id"] = str(comp.get("id") or "")
    side_ids = {"away": event["away_id"], "home": event["home_id"]}
    id_to_side = {v: k for k, v in side_ids.items() if v}
    boxes = {}
    for team_row in ((payload or {}).get("boxscore") or {}).get("teams") or []:
        side = team_row.get("homeAway")
        if side not in ("away", "home"):
            continue
        points = event["away_score"] if side == "away" else event["home_score"]
        parsed = team_box_from_stats(team_row.get("statistics"), points)
        if parsed:
            boxes[side] = parsed
    starters, active = {"away": [], "home": []}, {"away": [], "home": []}
    for block in ((payload or {}).get("boxscore") or {}).get("players") or []:
        tid = str((block.get("team") or {}).get("id") or "")
        side = id_to_side.get(tid)
        if not side:
            continue
        st, ac = _lineup(block)
        starters[side], active[side] = st, ac
    plays = (payload or {}).get("plays") or []
    timeouts = 0
    intentional = 0
    subs = 0
    for play in plays:
        typ = _play_type(play).lower()
        if "timeout" in typ:
            timeouts += 1
        if "intentional" in typ or "away from play" in typ:
            intentional += 1
        if typ == "substitution":
            subs += 1
    recent = _recent_box(plays, lambda tid: id_to_side.get(str(tid or "")), event["period"], event["clock_secs"]) if plays else None
    fouls_period = {"away": 0, "home": 0}
    if plays and event["period"]:
        partial, _ = _accumulate(plays, lambda tid: id_to_side.get(str(tid or "")), end_elapsed=_elapsed_sec(event["period"], event["clock_secs"] if event["clock_secs"] is not None else 0))
        for side in ("away", "home"):
            fouls_period[side] = partial[side]["fouls_period"]
    bonus = {}
    late = event["clock_secs"] is not None and event["clock_secs"] <= 120 and event["period"] == 4
    for side, n in fouls_period.items():
        bonus[side] = n >= 2 if late else n >= 5
    return {
        "event": event,
        "box": boxes,
        "quarters": event["linescores"],
        "recent_box": recent,
        "starters": starters,
        "active": active,
        "timeouts_taken": timeouts,
        "intentional_fouls": intentional,
        "substitutions": subs,
        "bonus": bonus,
        "plays": len(plays),
        "possession_side": event["possession_side"],
    }


def situations_agree(bdl, espn):
    """True when ESPN's score, and the live clock, match BALLDONTLIE."""
    if espn.get("away_score") is None or espn.get("home_score") is None:
        return False, "ESPN score missing"
    if int(bdl.get("away_score") or 0) != int(espn["away_score"]) or int(bdl.get("home_score") or 0) != int(espn["home_score"]):
        return False, (f"score mismatch BDL {bdl.get('away_score')}-{bdl.get('home_score')} "
                       f"vs ESPN {espn['away_score']}-{espn['home_score']}")
    bdl_state = bdl.get("state")
    if bdl.get("at_halftime") or (espn["state"] == "halftime"):
        if bdl.get("at_halftime") and espn["state"] == "halftime":
            return True, "halftime scores agree"
        return False, "halftime status mismatch"
    if bdl_state == "final" and espn["state"] == "final":
        return True, "final scores agree"
    if bdl_state != "in_progress" or espn["state"] != "in_progress":
        return False, f"status mismatch BDL {bdl_state} vs ESPN {espn['state']}"
    if int(bdl.get("period") or 0) != int(espn.get("period") or 0):
        return False, f"period mismatch BDL Q{bdl.get('period')} vs ESPN Q{espn.get('period')}"
    bdl_clock = bdl.get("clock_secs")
    espn_clock = espn.get("clock_secs")
    if bdl_clock is None or espn_clock is None:
        if bdl.get("at_end_q3") and "end of" in (espn.get("detail") or ""):
            return True, "end of period scores agree"
        return False, "clock missing on one feed"
    if abs(float(bdl_clock) - float(espn_clock)) > CLOCK_TOLERANCE_S:
        return False, f"clock mismatch BDL {bdl_clock:.0f}s vs ESPN {espn_clock:.0f}s"
    return True, "score and clock agree"


def _quarters_ok(lines, score):
    if not lines:
        return False
    return sum(lines) == score


def missing_after_espn(parsed, agree_ok):
    missing = []
    if not agree_ok or not parsed["box"].get("away") or not parsed["box"].get("home"):
        return list(model.MISSING_WITHOUT_BOX)
    if not parsed["plays"]:
        missing.append("play_by_play")
        missing.append("recent_window_efficiency")
        missing.append("intentional_fouls")
        missing.append("substitutions")
        missing.append("bonus")
    else:
        recent = parsed.get("recent_box") or {}
        if not recent or model.estimate_possessions(
                recent.get("away", {}).get("fga") or 0, recent.get("away", {}).get("oreb") or 0,
                recent.get("away", {}).get("tov") or 0, recent.get("away", {}).get("fta") or 0) < 4:
            missing.append("recent_window_efficiency")
    q_away = (parsed.get("quarters") or {}).get("away") or []
    q_home = (parsed.get("quarters") or {}).get("home") or []
    ev = parsed["event"]
    if not (_quarters_ok(q_away, ev.get("away_score")) and _quarters_ok(q_home, ev.get("home_score"))):
        missing.append("quarter_splits")
    active = parsed.get("active") or {}
    live_five = (parsed["event"]["state"] == "in_progress"
                 and len(active.get("away") or []) == 5
                 and len(active.get("home") or []) == 5)
    if not live_five:
        missing.append("current_lineup")
    if not parsed["possession_side"]:
        missing.append("possession_arrow")
    missing.append("timeouts_remaining")
    missing.append("season_team_ratings")
    return missing


def match_event(game, events):
    want = (team_key(game.get("away")), team_key(game.get("home")))
    for event in events:
        if (team_key(event.get("away")), team_key(event.get("home"))) == want:
            return event
    return None


def fetch_json(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), getattr(resp, "status", 200), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return None, exc.code, dict(exc.headers or {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None, 0, {}


def _cached(key, loader):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1], hit[2], True
    payload, code = loader()
    if payload is not None and code == 200:
        _CACHE[key] = (now, payload, code)
    return payload, code, False


def _bump_backoff(st, code):
    info = st.setdefault("espn", {"fails": 0, "backoff_until": 0})
    info["fails"] = int(info.get("fails") or 0) + 1
    wait = min(600, (120 if code == 429 else 30) * (2 ** min(info["fails"] - 1, 3)))
    info["backoff_until"] = time.time() + wait
    info["last_code"] = code
    return wait


def _clear_backoff(st):
    info = st.setdefault("espn", {"fails": 0, "backoff_until": 0})
    info["fails"] = 0
    info["backoff_until"] = 0


def _needs_box(game, st):
    if game.get("box"):
        return False
    if game.get("state") == "in_progress" or game.get("at_halftime") or game.get("at_end_q3"):
        return True
    gs = (st.get("games") or {}).get(game.get("id")) or {}
    return bool(gs.get("posted") and not gs.get("buyout_posted") and game.get("state") == "in_progress")


def attach_espn_boxes(games, st, slate, fetch=None):
    """Fill `box` on games whose ESPN summary agrees with BALLDONTLIE. Returns log lines."""
    logs = []
    if not any(_needs_box(g, st) for g in games):
        return logs
    info = st.setdefault("espn", {"fails": 0, "backoff_until": 0})
    if time.time() < float(info.get("backoff_until") or 0):
        logs.append("[Rusty Ryan] ESPN backoff active; pace fallback this poll")
        return logs
    fetch = fetch or (lambda url: fetch_json(url)[:2])
    day = slate.strftime("%Y%m%d")
    board, code, _cached_hit = _cached(
        f"board:{day}",
        lambda: fetch(f"{SCOREBOARD}?dates={day}"),
    )
    if code == 429:
        wait = _bump_backoff(st, 429)
        logs.append(f"[Rusty Ryan] ESPN scoreboard HTTP 429; backing off {wait:.0f}s; pace fallback")
        return logs
    if board is None:
        wait = _bump_backoff(st, code or 0)
        logs.append(f"[Rusty Ryan] ESPN scoreboard failed (HTTP {code}); backing off {wait:.0f}s; pace fallback")
        return logs
    events = parse_scoreboard(board)
    if any(_needs_box(g, st) and match_event(g, events) is None for g in games):
        nxt = (slate + timedelta(days=1)).strftime("%Y%m%d")
        board2, code2, _ = _cached(f"board:{nxt}", lambda: fetch(f"{SCOREBOARD}?dates={nxt}"))
        if code2 == 429:
            wait = _bump_backoff(st, 429)
            logs.append(f"[Rusty Ryan] ESPN scoreboard HTTP 429; backing off {wait:.0f}s; pace fallback")
            return logs
        if board2:
            events.extend(parse_scoreboard(board2))
    _clear_backoff(st)
    used = 0
    for game in games:
        if not _needs_box(game, st):
            continue
        event = match_event(game, events)
        if event is None:
            logs.append(f"[Rusty Ryan] ESPN has no game for {game.get('away')} @ {game.get('home')}; pace fallback")
            continue
        summary, scode, _ = _cached(
            f"summary:{event['id']}",
            lambda eid=event["id"]: fetch(f"{SUMMARY}?event={eid}"),
        )
        if scode == 429:
            wait = _bump_backoff(st, 429)
            logs.append(f"[Rusty Ryan] ESPN summary HTTP 429; backing off {wait:.0f}s; pace fallback")
            return logs
        if summary is None:
            logs.append(f"[Rusty Ryan] ESPN summary failed for {event['id']} (HTTP {scode}); pace fallback")
            continue
        parsed = parse_summary(summary)
        # A final summary often omits the clock. If the scoreboard still
        # matches the summary score, keep the scoreboard period and clock.
        ev = parsed["event"]
        if ev.get("clock_secs") is None and event.get("clock_secs") is not None:
            if ev.get("away_score") == event.get("away_score") and ev.get("home_score") == event.get("home_score"):
                ev["clock_secs"] = event["clock_secs"]
                if not ev.get("period"):
                    ev["period"] = event.get("period") or 0
        if not ev.get("period") and event.get("period"):
            ev["period"] = event["period"]
        ok, why = situations_agree(game, ev)
        if not ok:
            logs.append(f"[Rusty Ryan] ESPN ignored for {game.get('away')} @ {game.get('home')}: {why}; pace fallback")
            continue
        box = parsed["box"]
        if not box.get("away") or not box.get("home") or box["away"]["fga"] <= 0:
            logs.append(f"[Rusty Ryan] ESPN box empty for {game.get('away')} @ {game.get('home')}; pace fallback")
            continue
        game["box"] = {"away": dict(box["away"]), "home": dict(box["home"])}
        if parsed["event"]["state"] != "final" and parsed.get("recent_box"):
            game["recent_box"] = parsed["recent_box"]
        game["quarters"] = parsed.get("quarters")
        game["espn_context"] = {
            "event_id": event["id"],
            "possession_side": parsed.get("possession_side"),
            "bonus": parsed.get("bonus"),
            "timeouts_taken": parsed.get("timeouts_taken"),
            "intentional_fouls": parsed.get("intentional_fouls"),
            "substitutions": parsed.get("substitutions"),
            "active": parsed.get("active"),
            "starters": parsed.get("starters"),
            "plays": parsed.get("plays"),
            "agree": why,
        }
        game["missing_inputs"] = missing_after_espn(parsed, True)
        used += 1
        logs.append(
            f"[Rusty Ryan] ESPN box {game.get('away')} @ {game.get('home')} "
            f"FGA {box['away']['fga']:.0f}/{box['home']['fga']:.0f} "
            f"OREB {box['away']['oreb']:.0f}/{box['home']['oreb']:.0f} "
            f"TOV {box['away']['tov']:.0f}/{box['home']['tov']:.0f} "
            f"FTA {box['away']['fta']:.0f}/{box['home']['fta']:.0f} ({why})")
    if used:
        logs.append(f"[Rusty Ryan] ESPN possession inputs on {used} game(s)")
    return logs


def snapshot_from_plays(summary, elapsed_min):
    """Partial box and score at a game-clock mark, for historical checks only."""
    id_to_side = {}
    for team_row in (summary.get("boxscore") or {}).get("teams") or []:
        tid = str((team_row.get("team") or {}).get("id") or "")
        if tid and team_row.get("homeAway"):
            id_to_side[tid] = team_row["homeAway"]
    box, _last = _accumulate(summary.get("plays") or [], lambda tid: id_to_side.get(str(tid or "")),
                             end_elapsed=float(elapsed_min) * 60)
    return {
        "away_score": box["away"]["pts"],
        "home_score": box["home"]["pts"],
        "box": {
            "away": {k: box["away"][k] for k in ("fga", "fta", "oreb", "tov", "pts")},
            "home": {k: box["home"][k] for k in ("fga", "fta", "oreb", "tov", "pts")},
        },
        "event": parse_summary(summary)["event"],
    }
