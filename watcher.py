#!/usr/bin/env python3
"""Live NBA Over/Under watcher -- Richie's 5-agent crew, no LLM in the loop.

  1. Rusty Ryan        -- live game & pace scout (BALLDONTLIE /v1/games)
  2. Reuben Tishkoff   -- FanDuel totals tracker (The Odds API), quota guarded
  3. Yen               -- live totals quant (projection + edge)
  4. Frank Catton      -- risk auditor (blowout / close-game risk), final verdict
  5. Livingston Dell   -- Telegram formatter / poster

Modes:  (default) live run until slate is over
        --dry-run   print Telegram messages instead of posting
        --check     one BALLDONTLIE poll, print slate + trigger status, exit (no Odds/Telegram)
        --simulate  fabricated game states end to end, no network at all

Secrets come from the environment only:
  BALLDONTLIE_API_KEY, TELEGRAM_BOT_TOKEN, ODDS_API_KEY, TELEGRAM_CHANNEL_ID

On GitHub Actions, NBA_SLOT is slate | parlay-early | parlay-final | evening |
late | manual. A scheduled slot exits immediately when the America/New_York
clock is outside that slot's window (UTC cron fires twice, once per DST
offset). Parlay slots are one pass. State is read and written under
NBA_STATE_DIR so a later job on the same slate can resume.
"""
import argparse, csv, html, json, os, re, socket, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import espn_box
import model
import parlays
import recordbook

ET = ZoneInfo("America/New_York")
BDL_URL = "https://api.balldontlie.io/v1/games"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

# ---- tunables -------------------------------------------------------------
POLL_LIVE_S = 60          # BALLDONTLIE free tier ~5 req/min -> never faster than 60s
POLL_IDLE_S = 180         # nothing in progress yet
BASELINE_PPM = 4.75       # combined pts/min (~228 / 48)
PACE_WEIGHT = 0.6         # blended = 0.6*pace + 0.4*baseline
MIN_EDGE = 3.5          # full play in the Q4 8:00–5:00 window
LEAN_EDGE = 3.0         # lean in that same window
Q3_FULL_EDGE = 5.0      # full play from Q3 2:00 through the start of Q4
Q3_LEAN_EDGE = 4.5      # lean in that late-Q3 window
BLOWOUT_MARGIN = 22
CLOSE_MARGIN = 6
# The projection no longer applies a blowout pace trim or a close-game foul bump.
# Frank still passes OVER in a blowout and UNDER in a close game. Those used to
# move the number (0.93x and +3). They are a veto only now.
# Primary alert window: 4th quarter, 8:00 through 5:00 remaining.
# The last 2:00 is logged separately and is not an alert trigger.
# Late Q3 (2:00 left through the end of the quarter, plus the first minute
# of Q4) is a second window with a higher edge, because end-of-Q3 error is larger.
Q4_WINDOW_MAX = 8 * 60
Q4_WINDOW_MIN = 5 * 60
Q3_LATE_MAX = 2 * 60
Q4_OPEN_MIN = 11 * 60
CHECKPOINT_CLOCK = (
    ("q4_8:00", 8 * 60),
    ("q4_6:30", int(6.5 * 60)),
    ("q4_5:00", 5 * 60),
    ("q4_2:00", 2 * 60),
)
Q3_CHECKPOINT_CLOCK = (
    ("q3_2:00", 2 * 60),
)
LINE_STALE_S = 15 * 60
MAX_EVALS_PER_GAME = 2
REEVAL_GAP_S = 6 * 60
# 15 covered one pregame call plus the Q4 window. Late Q3 adds about one
# fetch per tip cluster (early / national / west), so the cap is 18.
# Expected spend is still ~6–9 credits a night, not the cap. Stop under 25.
MAX_ODDS_CALLS_PER_NIGHT = 18
MIN_CREDITS = 25
FINAL_STATES = {"final", "postponed", "canceled", "abandoned"}

# Scheduled jobs hand the runner off before GitHub's 6-hour kill.
# Slate starts ~12:05 PM ET and stops at 5:55 PM. Evening starts ~5:45 PM
# (queued until slate exits) and stops at 11:30 PM. The late job covers
# the rest until 3:00 AM, including West Coast finals around 1:30 AM.
SLATE_HANDOFF = (17, 55)     # 5:55 PM ET
EVENING_HANDOFF = (23, 30)   # 11:30 PM ET

# Clock windows are wide enough for a late cron and narrow enough that the
# other DST offset (one hour off, at the same minute) falls outside them.

DRY_RUN = False
_LOGGED_ONCE = set()


def now_et():
    return datetime.now(ET)


def state_dir():
    return os.environ.get("NBA_STATE_DIR") or os.environ.get("NBA_BASE") or os.path.dirname(os.path.abspath(__file__))


def log_path():
    return os.environ.get("NBA_LOG_PATH") or os.path.join(state_dir(), "watcher.log")


def redact(text):
    """Strip credentials that sometimes ride along in URLs or exception text."""
    text = str(text)
    text = re.sub(r"(?i)(apiKey=)[^&\s\"']+", r"\1REDACTED", text)
    text = re.sub(r"(?i)(api_key=)[^&\s\"']+", r"\1REDACTED", text)
    text = re.sub(r"(/bot)[0-9]+:[A-Za-z0-9_-]+", r"\1REDACTED", text)
    text = re.sub(r"(?i)Authorization:\s*\S+(?:\s+\S+)?", "Authorization: REDACTED", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer REDACTED", text)
    return text


def log(msg):
    line = f"{now_et():%Y-%m-%d %H:%M:%S %Z} | {redact(msg)}"
    print(line, flush=True)
    try:
        with open(log_path(), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_once(key, msg):
    if key in _LOGGED_ONCE:
        return
    _LOGGED_ONCE.add(key)
    log(msg)


def slate_date(dt=None):
    """NBA slate date = ET date of (now - 6h), so post-midnight games stay on the slate."""
    return ((dt or now_et()) - timedelta(hours=6)).date()


def slot_name():
    return (os.environ.get("NBA_SLOT") or "manual").strip() or "manual"


def slot_hour_ok(slot, now=None):
    """False when this process should no-op because the ET clock is the other DST offset."""
    if os.environ.get("NBA_PARLAY_FORCE") == "1" and slot in ("parlay-early", "parlay-final"):
        return True
    if slot in (None, "", "manual"):
        return True
    now = now or now_et()
    hm = (now.hour, now.minute)
    if slot == "slate":
        return now.hour == 12
    if slot == "evening":
        # 5:40 PM–6:40 PM. The other offset lands at 4:45 PM or 6:45 PM.
        if (17, 40) <= hm <= (17, 59):
            return True
        return now.hour == 18 and now.minute <= 40
    if slot == "late":
        # 11:00 PM–12:15 AM. The other offset lands at 10:20 PM or 12:20 AM.
        if now.hour == 23:
            return True
        return now.hour == 0 and now.minute <= 15
    if slot == "parlay-early":
        # 10:55–11:40 AM. The other offset of these crons is 10:08 AM or 12:08 PM.
        return (10, 55) <= hm <= (11, 40)
    if slot == "parlay-final":
        # 3:20–4:10 PM. The other offset is 2:30 PM or 4:30 PM.
        return (15, 20) <= hm <= (16, 10)
    return False


def handoff_due(slot, now):
    """True when this slot should exit so the next scheduled job can take the runner."""
    hm = (now.hour, now.minute)
    if slot == "slate":
        return hm >= SLATE_HANDOFF
    if slot == "evening":
        if hm >= EVENING_HANDOFF:
            return True
        # Past midnight the late job owns the night. Hour 0–5 is after 11:30 PM.
        if now.hour < 6:
            return True
    return False


# ---- secrets ----------------------------------------------------------------
def need_env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        log(f"FATAL: environment variable {name} is not set -- cannot run.")
        sys.exit(2)
    return v


def http_get(url, headers=None, timeout=20):
    # Callers must not log `url`: the Odds API key is a query parameter.
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), dict(r.headers)


# ---- state --------------------------------------------------------------------
def state_path(sd):
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"state-{sd.isoformat()}{suffix}.json")


def load_state(sd):
    p = state_path(sd)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    st = {"slate": sd.isoformat(), "pregame": {}, "pregame_done": False, "slate_posted": False,
          "odds_calls": 0, "credits_remaining": None, "games": {}}
    real = os.path.join(state_dir(), f"state-{sd.isoformat()}.json")
    if DRY_RUN and os.path.exists(real):  # reuse tonight's real pregame snapshot, don't spend a credit
        with open(real) as f:
            r = json.load(f)
        for k in ("pregame", "pregame_done", "odds_calls", "credits_remaining"):
            st[k] = r.get(k, st[k])
    return st


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# Quota fields are shared with the live state file so a dry run that takes the
# pregame snapshot does not make the live run buy it again. Posted flags are not
# copied: a dry run must not suppress the real Telegram card.
QUOTA_KEYS = (
    "pregame", "pregame_done", "pregame_meta", "odds_calls", "credits_remaining",
    "credits_used_month", "credits_by_feature", "prop_quotes", "prop_events_done",
)


def _mirror_quota_to_live(st):
    sd = datetime.fromisoformat(st["slate"]).date().isoformat()
    real_path = os.path.join(state_dir(), f"state-{sd}.json")
    if os.path.exists(real_path):
        with open(real_path) as f:
            real = json.load(f)
    else:
        real = {"slate": st["slate"], "pregame": {}, "pregame_done": False, "slate_posted": False,
                "odds_calls": 0, "credits_remaining": None, "games": {}}
    if all(real.get(k) == st.get(k) for k in QUOTA_KEYS) and os.path.exists(real_path):
        return
    for k in QUOTA_KEYS:
        real[k] = st.get(k)
    _write_json(real_path, real)


def save_state(st):
    sd = datetime.fromisoformat(st["slate"]).date()
    _write_json(state_path(sd), st)
    if DRY_RUN:
        _mirror_quota_to_live(st)


def heartbeat_path(sd):
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"heartbeat-{sd.isoformat()}{suffix}.txt")


def done_path(sd):
    return os.path.join(state_dir(), f"watcher-{sd.isoformat()}.done")


def write_heartbeat(sd):
    """Cheap per-tick liveness file. Not part of the persisted slate state."""
    p = heartbeat_path(sd)
    tmp = p + ".tmp"
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(tmp, "w") as f:
            f.write(str(int(time.time())) + "\n")
        os.replace(tmp, p)
    except OSError:
        pass


def write_done(sd):
    """Mark a normal shutdown (all final / 3 AM). A handoff does not write this."""
    if DRY_RUN:
        return
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(done_path(sd), "w") as f:
            f.write(f"done {int(time.time())}\n")
    except OSError:
        pass


def clear_done(sd):
    try:
        os.remove(done_path(sd))
    except FileNotFoundError:
        pass
    except OSError:
        pass


# =============================================================================
# AGENT 1 -- RUSTY RYAN: live game & pace scout
# =============================================================================
CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2}(?:\.\d+)?)")
SECS_RE = re.compile(r"^\s*(?:Q\d\s+)?(\d{1,2}(?:\.\d+)?)\s*$")
STATUS_PERIOD = {"1st qtr": 1, "2nd qtr": 2, "3rd qtr": 3, "4th qtr": 4}


def parse_clock(t):
    """Seconds left in the period from BALLDONTLIE `time`.
    Live formats handled: '6:42', 'Q3 6:42', '0:53.4', '53.4' (sub-minute, seconds).
    Returns None for '', ' ', 'Halftime', 'Final', 'End of 3rd', etc."""
    if not t or not isinstance(t, str):
        return None
    m = CLOCK_RE.search(t)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = SECS_RE.match(t)
    if m:
        return float(m.group(1))
    return None


def fmt_clock(secs):
    secs = max(0, int(round(secs)))
    return f"{secs // 60}:{secs % 60:02d}"


def scout(g):
    """Normalize one BALLDONTLIE game into the fields the crew needs."""
    away, home = g["visitor_team"]["full_name"], g["home_team"]["full_name"]
    a, h = g.get("visitor_team_score") or 0, g.get("home_team_score") or 0
    period = g.get("period") or 0
    state = (g.get("status_state") or "").lower()
    t = g.get("time")
    # 'Q3 6:42' style carries the period too -- trust it if present
    m = re.match(r"\s*Q(\d)", t or "")
    if m:
        period = int(m.group(1))
    sp = STATUS_PERIOD.get((g.get("status") or "").strip().lower())
    if sp and not period:
        period = sp
    clock = parse_clock(t)
    status_l = (g.get("status") or "").strip().lower()
    time_l = (t or "").strip().lower() if isinstance(t, str) else ""
    s = {"id": str(g["id"]), "away": away, "home": home,
         "away_nick": g["visitor_team"].get("name") or away.split()[-1],
         "home_nick": g["home_team"].get("name") or home.split()[-1], "away_score": a, "home_score": h,
         "total": a + h, "margin": abs(a - h), "period": period, "clock_secs": clock,
         "raw_time": t, "status": g.get("status"), "state": state,
         "tip": g.get("datetime"), "in_window": False, "in_q3_late": False, "at_halftime": False,
         "at_end_q3": False, "in_ot": False}
    box = {}
    if g.get("visitor_box"):
        box["away"] = g["visitor_box"]
    if g.get("home_box"):
        box["home"] = g["home_box"]
    if box:
        s["box"] = box
    if "halftime" in status_l or "halftime" in time_l:
        s["at_halftime"] = True
        s["elapsed_min"] = 24.0
        s["remaining_min"] = 24.0
        s["pace_ppm"] = s["total"] / 24.0 if s["total"] else 0.0
    elif "end of 3" in status_l or "end of 3" in time_l:
        s["at_end_q3"] = True
        s["in_q3_late"] = True
        s["elapsed_min"] = 36.0
        s["remaining_min"] = 12.0
        s["pace_ppm"] = s["total"] / 36.0 if s["total"] else 0.0
    elif state == "in_progress" and period in (1, 2, 3, 4) and clock is not None:
        elapsed = (period - 1) * 12 + (12 - clock / 60.0)
        s["elapsed_min"] = elapsed
        s["remaining_min"] = max(0.0, 48 - elapsed)
        s["pace_ppm"] = s["total"] / elapsed if elapsed > 0 else 0.0
        s["in_window"] = period == 4 and Q4_WINDOW_MIN <= clock <= Q4_WINDOW_MAX
        s["in_q3_late"] = (period == 3 and clock <= Q3_LATE_MAX) or (period == 4 and clock >= Q4_OPEN_MIN)
        if period == 3 and clock <= 20:
            s["at_end_q3"] = True
            s["in_q3_late"] = True
    elif state == "in_progress" and period and period > 4:
        s["in_ot"] = True
        s["elapsed_min"] = 48.0
        s["remaining_min"] = 0.0
        s["pace_ppm"] = s["total"] / 48.0 if s["total"] else 0.0
    return s


def rusty_poll(bdl_key, sd):
    # The key travels in the Authorization header, never in the URL or the log.
    url = f"{BDL_URL}?dates[]={sd.isoformat()}&per_page=100"
    for attempt in range(3):
        try:
            data, _ = http_get(url, {"Authorization": bdl_key})
            return [scout(g) for g in data.get("data", [])]
        except urllib.error.HTTPError as e:
            log(f"[Rusty Ryan] BALLDONTLIE HTTP {e.code} (attempt {attempt+1})")
            if e.code == 401:
                log("FATAL: [Rusty Ryan] BALLDONTLIE key rejected (401).")
                sys.exit(3)
            time.sleep(65 if e.code == 429 else 10)
        except Exception as e:
            log(f"[Rusty Ryan] BALLDONTLIE error: {redact(e)} (attempt {attempt+1})")
            time.sleep(10)
    return None


# =============================================================================
# AGENT 2 -- REUBEN TISHKOFF: FanDuel odds & line tracker (The Odds API)
# =============================================================================
def team_key(name):
    # Last word is unique across NBA teams and avoids 'LA Clippers' vs 'Los Angeles Clippers'
    return name.strip().split()[-1].lower()


def game_key(away, home):
    return f"{team_key(away)}@{team_key(home)}"


def _parse_odds_board(data):
    """FanDuel totals and spreads from one /odds payload. Spread is the absolute number."""
    lines = {}
    for ev in data or []:
        key = game_key(ev.get("away_team") or "", ev.get("home_team") or "")
        ln = {
            "commence": ev.get("commence_time"),
            "event_id": ev.get("id"),
            "away": ev.get("away_team"),
            "home": ev.get("home_team"),
        }
        for bk in ev.get("bookmakers") or []:
            if bk.get("key") != "fanduel":
                continue
            for mk in bk.get("markets") or []:
                name = mk.get("key")
                if name == "totals":
                    ln["last_update"] = mk.get("last_update")
                    for outcome in mk.get("outcomes") or []:
                        if outcome.get("name") == "Over":
                            ln["total"], ln["over"] = outcome.get("point"), outcome.get("price")
                        elif outcome.get("name") == "Under":
                            ln["under"] = outcome.get("price")
                            ln.setdefault("total", outcome.get("point"))
                elif name == "spreads":
                    points = []
                    for outcome in mk.get("outcomes") or []:
                        if outcome.get("point") is not None:
                            points.append(abs(float(outcome["point"])))
                    if points:
                        ln["spread"] = points[0]
        if ln.get("total") is not None or ln.get("spread") is not None:
            lines[key] = ln
    return lines


def reuben_fetch(st, odds_key, why):
    """One Odds API call (1 credit). Returns {game_key: line} or None if skipped/failed.

    The apiKey query parameter is never written to the log.
    """
    if st["odds_calls"] >= MAX_ODDS_CALLS_PER_NIGHT:
        log(f"[Reuben Tishkoff] QUOTA: nightly cap {MAX_ODDS_CALLS_PER_NIGHT} reached -- skipping Odds API ({why})")
        return None
    cr = st.get("credits_remaining")
    if cr is not None and cr < MIN_CREDITS:
        log(f"[Reuben Tishkoff] QUOTA: only {cr} credits left (< {MIN_CREDITS}) -- skipping Odds API ({why})")
        return None
    feature = "pregame" if str(why).startswith("pregame") else "live"
    # totals,spreads is 2 credits (markets × regions). A live fetch stays totals-only.
    markets = "totals,spreads" if feature == "pregame" else "totals"
    q = urllib.parse.urlencode({"apiKey": odds_key, "regions": "us", "markets": markets,
                                "bookmakers": "fanduel", "oddsFormat": "american"})
    st["odds_calls"] += 1
    try:
        data, hdr = http_get(f"{ODDS_URL}?{q}")
    except urllib.error.HTTPError as e:
        log(f"[Reuben Tishkoff] Odds API HTTP {e.code} ({why}); call #{st['odds_calls']}")
        save_state(st)
        return None
    except Exception:
        log(f"[Reuben Tishkoff] Odds API error (request failed) ({why}); call #{st['odds_calls']}")
        save_state(st)
        return None
    hl = {k.lower(): v for k, v in hdr.items()}
    try:
        st["credits_remaining"] = int(float(hl.get("x-requests-remaining")))
    except (TypeError, ValueError):
        pass
    try:
        st["credits_used_month"] = int(float(hl.get("x-requests-used")))
    except (TypeError, ValueError):
        pass
    try:
        cost = int(float(hl.get("x-requests-last")))
    except (TypeError, ValueError):
        cost = markets.count(",") + 1
    spent = st.setdefault("credits_by_feature", {})
    spent[feature] = int(spent.get(feature) or 0) + cost
    log(f"[Reuben Tishkoff] QUOTA: Odds API call #{st['odds_calls']} tonight ({why}, {feature} +{cost}); "
        f"x-requests-remaining={hl.get('x-requests-remaining')} used={hl.get('x-requests-used')}")
    lines = _parse_odds_board(data)
    if lines:
        st.setdefault("last_lines", {}).update(lines)
    save_state(st)
    return lines


def reuben_pregame(st, odds_key, games=None):
    """ONE pregame call per slate. totals+spreads, so the call costs 2 credits."""
    if st.get("pregame_done"):
        n = len(st.get("pregame") or {})
        # Once per process. This used to print on every poll tick.
        log_once("pregame-reuse",
                 f"[Reuben Tishkoff] reusing saved pregame totals for {n} games; no Odds API call")
        return
    pending = None
    if games is not None:
        pending = [g for g in games if g["state"] == "scheduled"]
        if not pending:
            st["pregame_done"] = True
            save_state(st)
            log("[Reuben Tishkoff] pregame snapshot skipped -- no games still scheduled")
            return
    lines = reuben_fetch(st, odds_key, "pregame snapshot")
    st["pregame_done"] = True
    meta = st.setdefault("pregame_meta", {})
    if lines:
        for key, ln in lines.items():
            if ln.get("total") is not None:
                st["pregame"][key] = ln["total"]
            meta[key] = {
                "key": key,
                "total": ln.get("total"),
                "spread": ln.get("spread"),
                "event_id": ln.get("event_id"),
                "away": ln.get("away"),
                "home": ln.get("home"),
            }
        if pending is not None:
            for g in pending:
                k = game_key(g["away"], g["home"])
                if k in lines and lines[k].get("total") is not None:
                    spread = lines[k].get("spread")
                    extra = f", spread {spread:g}" if spread is not None else ""
                    log(f"[Reuben Tishkoff] pregame FanDuel total {g['away']} @ {g['home']}: {lines[k]['total']}{extra}")
                else:
                    log(f"[Reuben Tishkoff] pregame: no FanDuel total yet for {g['away']} @ {g['home']}")
    save_state(st)


def reuben_props(st, odds_key, chosen):
    """One FanDuel prop request per chosen game. 3 markets × 1 region = 3 credits each."""
    if st.get("props_done"):
        log_once("props-reuse", "[Reuben Tishkoff] reusing saved prop quotes; no Odds API call")
        return st.get("prop_quotes") or {}
    if DRY_RUN or not odds_key:
        log("[Reuben Tishkoff] prop prices not purchased on a dry run")
        return st.get("prop_quotes") or {}
    bag = st.get("credits_by_feature") or {}
    if not parlays.props_allowed(
            st.get("credits_remaining"), st.get("credits_used_month"), now_et().date(),
            live_spent_today=int(bag.get("live") or 0),
            pregame_spent_today=int(bag.get("pregame") or 0)):
        log(f"[Reuben Tishkoff] prop check skipped -- credits left {st.get('credits_remaining')} "
            f"(floor {parlays.PROP_MIN_REMAINING}, month cap {parlays.PLAN_CREDITS})")
        st["props_done"] = True
        save_state(st)
        return {}
    quotes = dict(st.get("prop_quotes") or {})
    done = set(str(item) for item in (st.get("prop_events_done") or []))
    failed = False
    for game in chosen:
        event_id = str(game.get("event_id") or "")
        if not event_id or event_id in done:
            continue
        if st["odds_calls"] >= MAX_ODDS_CALLS_PER_NIGHT:
            log(f"[Reuben Tishkoff] QUOTA: nightly cap {MAX_ODDS_CALLS_PER_NIGHT} reached -- skipping props")
            failed = True
            break
        cr = st.get("credits_remaining")
        if cr is not None and cr < MIN_CREDITS:
            log(f"[Reuben Tishkoff] QUOTA: only {cr} credits left (< {MIN_CREDITS}) -- skipping props")
            failed = True
            break
        q = urllib.parse.urlencode({
            "apiKey": odds_key, "regions": "us",
            "markets": "player_points,player_rebounds,player_assists",
            "bookmakers": "fanduel", "oddsFormat": "american",
        })
        url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/"
               f"{urllib.parse.quote(event_id)}/odds?{q}")
        st["odds_calls"] += 1
        try:
            data, hdr = http_get(url)
        except urllib.error.HTTPError as exc:
            log(f"[Reuben Tishkoff] Odds API HTTP {exc.code} (props); call #{st['odds_calls']}")
            failed = True
            save_state(st)
            continue
        except Exception:
            log(f"[Reuben Tishkoff] Odds API error (props request failed); call #{st['odds_calls']}")
            failed = True
            save_state(st)
            continue
        hl = {k.lower(): v for k, v in hdr.items()}
        try:
            st["credits_remaining"] = int(float(hl.get("x-requests-remaining")))
        except (TypeError, ValueError):
            pass
        try:
            st["credits_used_month"] = int(float(hl.get("x-requests-used")))
        except (TypeError, ValueError):
            pass
        try:
            cost = int(float(hl.get("x-requests-last")))
        except (TypeError, ValueError):
            cost = 3
        spent = st.setdefault("credits_by_feature", {})
        spent["props"] = int(spent.get("props") or 0) + cost
        log(f"[Reuben Tishkoff] QUOTA: props call #{st['odds_calls']} cost {cost}; "
            f"x-requests-remaining={hl.get('x-requests-remaining')}")
        parsed = parlays.parse_prop_payload(data)
        quotes[event_id] = parsed
        if game.get("key"):
            quotes[game["key"]] = parsed
        done.add(event_id)
        st["prop_quotes"] = quotes
        st["prop_events_done"] = sorted(done)
        save_state(st)
    wanted = {str(game.get("event_id")) for game in chosen if game.get("event_id")}
    if not failed and wanted <= done:
        st["props_done"] = True
        save_state(st)
    return quotes


# =============================================================================
# AGENT 3 -- YEN: live totals quant;  AGENT 4 -- FRANK CATTON: risk + verdict
# =============================================================================
def alert_window(g):
    """q4 is the primary window. q3_late is 2:00 left in Q3 through the first minute of Q4."""
    if g.get("in_window"):
        return "q4"
    if g.get("in_q3_late"):
        return "q3_late"
    return None


def edge_cutoffs(g):
    """Full-play edge, then lean edge, for this clock."""
    if g.get("in_q3_late") and not g.get("in_window"):
        return Q3_FULL_EDGE, Q3_LEAN_EDGE
    return MIN_EDGE, LEAN_EDGE


def yen_and_frank(g, line, pregame_total=None):
    """Project the full-game total, then apply Frank's vetoes.

    The projection does not slow a blowout or add points for a close game.
    Frank passes an OVER when the margin is 22+ and an UNDER when the margin
    is 6 or less. Q4 needs 3.5 for a play and 3.0 for a lean. Late Q3 needs
    5.0 and 4.5 because that checkpoint has been noisier.
    """
    pack = model.project_game(g, pregame_total)
    proj = pack["proj"]
    edge = proj - line["total"]
    side = "OVER" if edge > 0 else "UNDER"
    p_over, p_under, p_push = model.ou_probabilities(proj, line["total"], pack["sd"])
    blowout = g["margin"] >= BLOWOUT_MARGIN
    close = g["margin"] <= CLOSE_MARGIN
    full_edge, lean_edge = edge_cutoffs(g)
    if abs(edge) >= full_edge:
        tier = "play"
    elif abs(edge) >= lean_edge:
        tier = "lean"
    else:
        tier = None
    verdict, reason = side, ""
    if tier is None:
        verdict, reason = "PASS / NO PLAY", f"edge {edge:+.1f} < {lean_edge}"
    elif blowout and side == "OVER":
        verdict, reason = "PASS / NO PLAY", "blowout contradicts OVER"
        tier = None
    elif close and side == "UNDER":
        verdict, reason = "PASS / NO PLAY", "close-game fouling contradicts UNDER"
        tier = None
    if verdict == "PASS / NO PLAY" and blowout and side == "OVER":
        note = (f"{g['margin']}-point margin. Frank passes OVER in a blowout of {BLOWOUT_MARGIN}+. "
                f"The projection itself is not slowed down for the blowout.")
    elif verdict == "PASS / NO PLAY" and close and side == "UNDER":
        note = (f"{g['margin']}-point game. Frank passes UNDER within {CLOSE_MARGIN}. "
                f"The projection does not add a hard-coded foul bump.")
    elif pack["model"] == "possession_estimate":
        note = (f"{g['margin']}-point game. Possession estimate "
                f"(FGA−OREB+TOV+0.44·FTA), shrunk toward the league baseline. "
                f"No Frank veto.")
    else:
        note = (f"{g['margin']}-point game. Pace fallback (no live possession totals). "
                f"No Frank veto.")
    out = {
        "pace": pack.get("pace") or 0.0,
        "rate": (pack.get("detail") or {}).get("fallback_rate_ppm") or pack.get("pace") or 0.0,
        "adj_rate": (pack.get("detail") or {}).get("fallback_rate_ppm") or pack.get("pace") or 0.0,
        "raw_proj": pack["proj_regulation"],
        "proj": proj,
        "edge": edge,
        "blowout": blowout,
        "close": close,
        "verdict": verdict,
        "side": side,
        "tier": tier,
        "reason": reason,
        "note": note,
        "p_over": p_over,
        "p_under": p_under,
        "p_push": p_push,
    }
    out.update(pack)
    out["proj"] = proj
    return out


# =============================================================================
# AGENT 5 -- LIVINGSTON DELL: Telegram formatter
# =============================================================================
def am(p):
    if p is None:
        return "n/a"
    p = int(round(float(p)))
    return f"+{p}" if p > 0 else str(p)


def fmt_num(x):
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def fmt_prob(p):
    if p is None:
        return "n/a"
    if p >= 0.99:
        return ">99%"
    if p <= 0.01:
        return "<1%"
    return f"{p:.0%}"


def dell_format(g, line, res, pregame_total=None):
    e = html.escape
    qtxt = f"Q{g['period']} - {fmt_clock(g['clock_secs'])} left"
    move = ""
    if pregame_total is not None:
        d = line["total"] - pregame_total
        move = f" (opened {fmt_num(pregame_total)}, {d:+g})"
    side = res.get("side") or res["verdict"]
    odds = line["over"] if side == "OVER" else line["under"]
    call = f"{side} {fmt_num(line['total'])}"
    if res.get("tier") == "lean":
        call_line = f"⚠️ LEAN: <b>{call}</b> ({am(odds)})"
    else:
        call_line = f"✅ FINAL CALL: <b>{call}</b> ({am(odds)})"
    side_p = res.get("p_over") if side == "OVER" else res.get("p_under")
    extra = ""
    if res.get("lo_80") is not None and res.get("tier") in ("play", "lean") and side_p is not None:
        kind = "possession estimate" if res.get("model") == "possession_estimate" else "pace fallback"
        extra = (f"📐 UNCALIBRATED 80% {res['lo_80']:.0f}–{res['hi_80']:.0f}"
                 f" · P({side}) {fmt_prob(side_p)} · full-game incl. OT · {kind}\n")
    return (f"🏀 {e(g['away'])} @ {e(g['home'])} | {qtxt} | Score: {g['away_score']}-{g['home_score']}\n"
            f"📊 FanDuel Live Line: Total <b>{fmt_num(line['total'])}</b> "
            f"(Over {am(line['over'])} / Under {am(line['under'])}){move}\n"
            f"🤖 Yen's Projected Total: <b>{res['proj']:.1f}</b>\n"
            f"{call_line}\n"
            f"{extra}"
            f"📝 Frank's Inside Note: {e(res['note'])}")


def dell_buyout(g, gs, proj, p_side):
    e = html.escape
    if g.get("clock_secs") is not None:
        clock = f"{fmt_clock(g['clock_secs'])} left"
    else:
        clock = g.get("raw_time") or "—"
    return (f"⚠️ TAKE THE BUYOUT\n"
            f"🏀 {e(g['away'])} @ {e(g['home'])} | Q{g['period']} - {clock} | "
            f"Score: {g['away_score']}-{g['home_score']}\n"
            f"Original call: <b>{'LEAN ' if gs.get('posted_tier') == 'lean' else ''}{gs['posted_side']} {fmt_num(gs['posted_line'])}</b>\n"
            f"Updated projection: <b>{proj:.1f}</b> vs <b>{fmt_num(gs['posted_line'])}</b> "
            f"(UNCALIBRATED P({gs['posted_side']}) {fmt_prob(p_side)})\n"
            f"Advisory only. FanDuel cash-out prices are not in the API.")


def parlays_path():
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"parlays{suffix}.json")


def load_parlays():
    path = parlays_path()
    if os.path.exists(path):
        with open(path) as f:
            document = json.load(f)
        document.setdefault("parlays", [])
        return document
    return parlays.empty_parlays()


def save_parlays(document):
    _write_json(parlays_path(), document)


def post_parlay_board(board, st, sd, tg_token, channel, odds_key):
    """Post one board and exit. Early spends 0 odds credits. Final may buy props."""
    flag = "early_board_posted" if board == "early" else "final_board_posted"
    if st.get(flag) or os.environ.get("NBA_SKIP_PARLAYS") == "1":
        return
    quotes = {}
    if board == "final":
        reuben_pregame(st, odds_key, None)
        chosen, how = parlays.pick_prop_games(list((st.get("pregame_meta") or {}).values()))
        names = ", ".join(
            f"{row.get('away')} @ {row.get('home')} (spread {row.get('spread')})" for row in chosen)
        log(f"[Reuben Tishkoff] prop games by {how}: {names or 'none'}")
        quotes = reuben_props(st, odds_key, chosen) or {}
    try:
        built = parlays.build_for_slate(
            sd, st.get("pregame") or {}, board=board, prop_quotes=quotes or None)
    except Exception as exc:
        log(f"[Livingston Dell] parlay build failed: {type(exc).__name__}")
        return
    if built is None:
        log("[Livingston Dell] parlay board unavailable -- will retry")
        return
    document = load_parlays()
    early = [row for row in document["parlays"]
             if row.get("slate") == sd.isoformat() and row.get("board") == "early"]
    changes = parlays.change_note(early, built) if board == "final" else None
    if not built:
        log(f"[Livingston Dell] no {board} parlay cleared the floor")
        st[flag] = True
        save_state(st)
        return
    document["parlays"] = [
        row for row in document["parlays"]
        if not (row.get("slate") == sd.isoformat() and row.get("board") == board)
    ] + built
    save_parlays(document)
    for text in parlays.render_parlays(built, board, changes):
        if not dell_post(text, tg_token, channel):
            log("[Livingston Dell] FAILED to post parlay board -- will retry")
            return
    st[flag] = True
    save_state(st)
    log(f"[Livingston Dell] posted {board} board ({len(built)} parlays){' (dry run)' if DRY_RUN else ''}")


def maybe_final_board_from_slate(st, sd, tg_token, channel, odds_key):
    """Day-game slate holds the runner, so it posts the 3:30 board itself."""
    if st.get("final_board_posted") or os.environ.get("NBA_SKIP_PARLAYS") == "1":
        return
    if not slot_hour_ok("parlay-final", now_et()):
        return
    post_parlay_board("final", st, sd, tg_token, channel, odds_key)


_PARLAY_BOXES = {}


def grade_open_parlays(games):
    """Grade totals from the scoreboard in hand, and player legs only after the final box."""
    document = load_parlays()
    if not document["parlays"]:
        return
    finals = {}
    final_pairs = set()
    open_pairs = set()
    for game in games:
        pair = frozenset(((game.get("away_nick") or "").lower(), (game.get("home_nick") or "").lower()))
        if game.get("state") == "final":
            final_pairs.add(pair)
        else:
            open_pairs.add(pair)
    player_boxes = {}
    if os.environ.get("NBA_SKIP_PARLAYS") != "1":
        needed = {}
        for parlay in parlays.active_parlays(document):
            for leg in parlay.get("legs") or []:
                if leg.get("kind") == "player" and not leg.get("result") and leg.get("event_id"):
                    needed.setdefault(str(leg["event_id"]), leg)
        for event_id, leg in needed.items():
            if event_id in _PARLAY_BOXES:
                player_boxes[event_id] = _PARLAY_BOXES[event_id]
                continue
            pair = frozenset((
                recordbook._nick(leg.get("team") or "").lower(),
                recordbook._nick(leg.get("opponent") or "").lower(),
            ))
            if pair in open_pairs and pair not in final_pairs:
                continue
            payload, code, _ = espn_box.fetch_json(f"{espn_box.SUMMARY}?event={event_id}")
            if not payload or code != 200 or not parlays.summary_is_final(payload):
                continue
            player_boxes[event_id] = _PARLAY_BOXES[event_id] = parlays.player_stats_from_summary(payload)
    for game in games:
        if game.get("state") != "final":
            continue
        finals[str(game.get("id"))] = game.get("total")
        finals[f"{(game.get('away_nick') or '').lower()}@{(game.get('home_nick') or '').lower()}"] = game.get("total")
    changed = False
    for parlay in parlays.active_parlays(document):
        for leg in parlay.get("legs") or []:
            if leg.get("result"):
                continue
            if leg.get("kind") == "total":
                actual = finals.get(str(leg.get("event_id")))
                if actual is None:
                    actual = finals.get(f"{(leg.get('away_nick') or '').lower()}@{(leg.get('home_nick') or '').lower()}")
            else:
                stats = (player_boxes.get(str(leg.get("event_id"))) or {}).get(str(leg.get("player_id"))) or {}
                actual = stats.get(leg.get("stat"))
            result = parlays.grade_leg(leg, actual)
            if result:
                leg["result"] = result
                changed = True
        result = parlays.grade_parlay(parlay)
        if result and parlay.get("result") != result:
            parlay["result"] = result
            changed = True
    if changed:
        save_parlays(document)


def picks_record_path():
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"record{suffix}.json")


def load_picks_record():
    path = picks_record_path()
    if os.path.exists(path):
        with open(path) as f:
            record = json.load(f)
        record.setdefault("calls", [])
        record.setdefault("confirmed", [])
        return record
    return recordbook.empty_record()


def save_picks_record(record):
    _write_json(picks_record_path(), record)


_ESPN_FINAL_CACHE = {}


def espn_final_for(call):
    """Final score from the public scoreboard for an older slate. None if it is not final."""
    slate = call.get("slate")
    if not slate or os.environ.get("NBA_SKIP_RECORD_FETCH") == "1":
        return None
    if slate not in _ESPN_FINAL_CACHE:
        payload, code, _ = espn_box.fetch_json(
            f"{espn_box.SCOREBOARD}?dates={slate.replace('-', '')}")
        events = espn_box.parse_scoreboard(payload) if payload and code == 200 else []
        _ESPN_FINAL_CACHE[slate] = events
    event = espn_box.match_event({"away": call.get("away"), "home": call.get("home")}, _ESPN_FINAL_CACHE[slate])
    if event and event.get("state") == "final" and event.get("away_score") is not None:
        return event["away_score"], event["home_score"]
    return None


def refresh_picks_record(st, games):
    """Grade finished calls, including seeds and any night whose state file is on disk."""
    record = load_picks_record()
    recordbook.ensure_seeds(record)
    recordbook.ingest_state(record, st, games)
    for name in os.listdir(state_dir()) if os.path.isdir(state_dir()) else []:
        if not name.startswith("state-") or not name.endswith(".json") or name.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(state_dir(), name)) as f:
                saved = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        recordbook.ingest_state(record, saved, games)
    recordbook.ingest_confirmed(record, _confirmed_rows())
    extra = {}
    by_id = recordbook.games_index(games)
    for bucket in (record["calls"], record.get("confirmed") or []):
        for call in bucket:
            if call.get("result") in ("win", "loss", "push"):
                continue
            gid = str(call.get("game_id"))
            game = by_id.get(gid)
            if game and game.get("state") != "final":
                continue
            if game and game.get("state") == "final":
                continue
            if gid in recordbook.SEED_FINALS:
                continue
            scores = espn_final_for(call)
            if scores:
                extra[gid] = scores
    recordbook.grade_ready(record, by_id, extra)
    save_picks_record(record)
    wins, losses, pushes = recordbook.tally(record["calls"])
    log_once("picks-record",
             f"[Livingston Dell] CourtSide Picks {recordbook.record_string(wins, losses, pushes)}")
    return record


def _confirmed_rows():
    path = os.path.join(state_dir(), "confirmed-lines.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def dell_slate_text(games, pregame, sd, record=None, credits=None):
    """Livingston Dell's daily slate card. games = scouted dicts; pregame = {game_key: total}."""
    gs = sorted(games, key=lambda g: g["tip"] or "9999")
    tips = [datetime.fromisoformat(g["tip"].replace("Z", "+00:00")).astimezone(ET) for g in gs if g["tip"]]
    when = "Today's" if tips and tips[0].hour < 17 else "Tonight's"
    rows = []
    for g in gs:
        t = tip_et(g["tip"]) if g["tip"] else "TBD"
        tot = pregame.get(game_key(g["away"], g["home"]))
        ou = f" (O/U {fmt_num(tot)})" if tot is not None else ""
        rows.append(f"{t} – {html.escape(g['away_nick'])} @ {html.escape(g['home_nick'])}{ou}")
    brand = ""
    if record:
        brand = recordbook.format_header(record, sd, parlays.parlay_header(load_parlays()))
    if credits is None:
        credit = "Odds credits: not checked yet"
    else:
        credit = f"Odds credits: {credits} left"
    return (brand + f"🏀 {when} NBA Slate ({sd:%b} {sd.day})\n\n" + "\n".join(rows) +
            f"\n\n{credit}"
            "\nLive Over/Under picks drop here in the 4th quarter, from 8:00 to 5:00 👀")


def dell_post_slate(st, games, sd, tg_token, channel):
    """Post the slate once per slate date (state flag slate_posted)."""
    record = refresh_picks_record(st, games)
    if st.get("slate_posted") or not games:
        return
    text = dell_slate_text(games, st.get("pregame", {}), sd, record, st.get("credits_remaining"))
    if dell_post(text, tg_token, channel):
        st["slate_posted"] = True
        log(f"[Livingston Dell] slate posted ({len(games)} games){' (dry run)' if DRY_RUN else ''}")
    else:
        log("[Livingston Dell] FAILED to post slate -- will retry next poll")
    save_state(st)


def dell_post(text, tg_token, channel):
    if DRY_RUN:
        print("----- [Livingston Dell] DRY RUN, would post: -----\n" + text + "\n" + "-" * 50, flush=True)
        return True
    body = urllib.parse.urlencode({"chat_id": channel, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    # Token is in the path. Log only the status code, never the request URL.
    req = urllib.request.Request(f"https://api.telegram.org/bot{tg_token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read().decode()).get("ok")
            return bool(ok)
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description")
        except Exception:
            desc = ""
        log(f"[Livingston Dell] Telegram HTTP {e.code}: {redact(desc)}")
    except Exception:
        log("[Livingston Dell] Telegram error (request failed)")
    return False


# =============================================================================
# main loop
# =============================================================================
def due_for_eval(gs, now_ts):
    if gs.get("posted"):
        return False
    n = gs.get("evals", 0)
    if n == 0:
        return True
    if n >= MAX_EVALS_PER_GAME:
        return False
    return now_ts - gs.get("last_eval_ts", 0) >= REEVAL_GAP_S


def evaluate(g, line, st, tg_token, channel):
    gs = st["games"].setdefault(g["id"], {})
    gs["evals"] = gs.get("evals", 0) + 1
    gs["last_eval_ts"] = time.time()
    tag = f"{g['away']} @ {g['home']} Q{g['period']} {fmt_clock(g['clock_secs'])} {g['away_score']}-{g['home_score']}"
    if not line:
        log(f"[Frank Catton] PASS / NO PLAY | {tag} | no FanDuel live total available (eval {gs['evals']})")
        save_state(st)
        return
    pre = st["pregame"].get(game_key(g["away"], g["home"]))
    res = yen_and_frank(g, line, pre)
    _log_projection(g, res, line, pre, tag)
    log(f"[Frank Catton] {res['verdict']} | {tag} | blowout={res['blowout']} close={res['close']} "
        f"{res['reason']} | model={res['model']} | eval {gs['evals']}/{MAX_EVALS_PER_GAME}")
    gs.setdefault("decisions", []).append({"at": now_et().isoformat(), "verdict": res["verdict"],
                                           "proj": round(res["proj"], 1), "line": line["total"],
                                           "edge": round(res["edge"], 1), "model": res["model"]})
    if res.get("tier") in ("play", "lean"):
        msg = dell_format(g, line, res, pre)
        if dell_post(msg, tg_token, channel):
            gs["posted"] = True
            gs["posted_tier"] = res["tier"]
            gs["posted_side"] = res["side"]
            gs["posted_line"] = line["total"]
            gs["posted_price"] = line["over"] if res["side"] == "OVER" else line["under"]
            gs["away"] = g["away"]
            gs["home"] = g["home"]
            gs["away_nick"] = g["away_nick"]
            gs["home_nick"] = g["home_nick"]
            log(f"[Livingston Dell] posted {res['tier']} {res['side']} {fmt_num(line['total'])} for {g['away']} @ {g['home']}"
                f"{' (dry run)' if DRY_RUN else ''}")
        else:
            log(f"[Livingston Dell] FAILED to post {res['verdict']} for {g['away']} @ {g['home']}")
    save_state(st)


def _log_projection(g, res, line, pre, tag):
    mv = f" move {line['total'] - pre:+g} (pregame {pre})" if pre is not None else " move n/a"
    miss = ",".join(res.get("missing_inputs") or [])
    log(f"[Yen] {tag} | model={res['model']} {res['calibration']} | "
        f"reg {res['proj_regulation']:.1f} + OT {res['expected_ot_points']:.1f} "
        f"-> full {res['proj']:.1f} (80% {res['lo_80']:.1f}–{res['hi_80']:.1f}) "
        f"vs FanDuel {line['total']} (O {am(line.get('over'))}/U {am(line.get('under'))}){mv} "
        f"edge {res['edge']:+.1f} P(OVER)={res['p_over']:.2f} P(UNDER)={res['p_under']:.2f} "
        f"P(push)={res['p_push']:.2f} | missing: {miss}")
    for note in res.get("notes") or []:
        log(f"[Yen] {tag} | {note}")


def line_for(g, st, fresh):
    """Prefer the line just fetched. Otherwise reuse the last stored line. No extra credit."""
    key = game_key(g["away"], g["home"])
    if fresh and key in fresh:
        return fresh[key], "fresh"
    cached = (st.get("last_lines") or {}).get(key)
    if cached and cached.get("total") is not None:
        return cached, "reused"
    pre = (st.get("pregame") or {}).get(key)
    if pre is not None:
        return {"total": pre, "over": None, "under": None, "last_update": None}, "pregame_only"
    return None, "unavailable"


def _parse_update_age(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return time.time() - dt.timestamp()


def checkpoint_names(g, gs):
    """Checkpoints whose clock we are on, or that we crossed since the last poll."""
    done = set(gs.get("checkpoints") or [])
    found = []
    if g.get("at_halftime") and "halftime" not in done:
        found.append(("halftime", "on_time"))
    if g.get("at_end_q3") and "end_q3" not in done:
        found.append(("end_q3", "on_time"))
    clock = g.get("clock_secs")
    if g.get("period") == 3 and clock is not None:
        nearest_name, nearest_target = min(Q3_CHECKPOINT_CLOCK, key=lambda item: abs(clock - item[1]))
        if nearest_name not in done and abs(clock - nearest_target) <= 70:
            found.append((nearest_name, "on_time"))
    if g.get("period") == 4 and clock is not None:
        last = gs.get("last_clock")
        last_period = gs.get("last_period")
        nearest_name, nearest_target = min(CHECKPOINT_CLOCK, key=lambda item: abs(clock - item[1]))
        if nearest_name not in done and abs(clock - nearest_target) <= 70:
            found.append((nearest_name, "on_time"))
        if last_period == 4 and last is not None and last > clock and (last - clock) <= 150:
            for name, target in CHECKPOINT_CLOCK:
                if name in done or any(hit[0] == name for hit in found):
                    continue
                if last >= target > clock:
                    found.append((name, "crossed"))
    return found


def _quality(g, line, source):
    issues = []
    if g.get("away_score") is None or g.get("home_score") is None:
        issues.append("score_missing")
    elif (g.get("away_score") or 0) + (g.get("home_score") or 0) != g.get("total"):
        issues.append("score_inconsistent")
    if g.get("period") == 4 and g.get("clock_secs") is None:
        issues.append("clock_missing")
    if line is None or line.get("total") is None:
        issues.append("line_missing")
    else:
        if line.get("over") is None or line.get("under") is None:
            issues.append("price_missing")
        age = _parse_update_age(line.get("last_update"))
        if age is None:
            issues.append("line_timestamp_unknown")
        elif age > LINE_STALE_S:
            issues.append("line_stale")
    if not g.get("box"):
        issues.append("lineup_missing")
    else:
        issues.append("lineup_not_live")  # box minutes are game totals, not the on-court five
    return issues, source


def checkpoints_path(sd):
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"checkpoints-{sd.isoformat()}{suffix}.json")


def grades_path(sd):
    suffix = "-dryrun" if DRY_RUN else ""
    return os.path.join(state_dir(), f"grades-{sd.isoformat()}{suffix}.json")


def load_checkpoints(sd):
    p = checkpoints_path(sd)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"slate": sd.isoformat(), "calibration": model.CALIBRATION, "records": []}


def save_checkpoints(sd, doc):
    _write_json(checkpoints_path(sd), doc)


def confirmed_index(sd):
    """Rows Richie recorded, keyed by (game_id, checkpoint)."""
    path = os.path.join(state_dir(), "confirmed-lines.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("slate") or "") != sd.isoformat():
                continue
            key = (str(row.get("game_id") or ""), row.get("checkpoint") or "")
            out[key] = row
    return out


def record_checkpoints(g, st, sd, fresh_lines):
    gs = st["games"].setdefault(g["id"], {})
    names = checkpoint_names(g, gs)
    gs["last_clock"] = g.get("clock_secs")
    gs["last_period"] = g.get("period")
    if not names:
        return
    line, source = line_for(g, st, fresh_lines)
    issues, source = _quality(g, line, source)
    if "score_missing" in issues or "score_inconsistent" in issues:
        log(f"[Yen] checkpoint skipped for {g['away']} @ {g['home']}: score not consistent")
        return
    if "clock_missing" in issues and g.get("period") == 4:
        log(f"[Yen] checkpoint skipped for {g['away']} @ {g['home']}: clock missing")
        return
    pre = st.get("pregame", {}).get(game_key(g["away"], g["home"]))
    market = line if line and line.get("total") is not None and source != "unavailable" else None
    # Probabilities need a real number. Without one, still store the projection.
    priced = market or {"total": g["total"], "over": None, "under": None}
    res = yen_and_frank(g, priced, pre)
    doc = load_checkpoints(sd)
    confirmed = confirmed_index(sd)
    logged = set(gs.get("checkpoints") or [])
    for name, timing in names:
        if name in logged:
            continue
        side = "OVER" if market and res["proj"] >= market["total"] else ("UNDER" if market else None)
        row = {
            "game_id": g["id"], "away": g["away"], "home": g["home"],
            "checkpoint": name, "timing": timing,
            "at": now_et().isoformat(),
            "period": g.get("period"), "clock_secs": g.get("clock_secs"),
            "away_score": g.get("away_score"), "home_score": g.get("home_score"),
            "model": res["model"], "calibration": model.CALIBRATION,
            "settlement": res["settlement"],
            "proj": round(res["proj"], 2),
            "proj_regulation": round(res["proj_regulation"], 2),
            "expected_ot_points": round(res["expected_ot_points"], 2),
            "lo_80": round(res["lo_80"], 2), "hi_80": round(res["hi_80"], 2),
            "interval_label": model.CALIBRATION,
            "p_over": round(res["p_over"], 4) if market else None,
            "p_under": round(res["p_under"], 4) if market else None,
            "p_push": round(res["p_push"], 4) if market else None,
            "prob_label": model.CALIBRATION,
            "model_side": side,
            "line_total": market["total"] if market else None,
            "line_over": market.get("over") if market else None,
            "line_under": market.get("under") if market else None,
            "line_status": source if market else "unavailable",
            "line_last_update": market.get("last_update") if market else None,
            "quality_issues": issues,
            "missing_inputs": res["missing_inputs"],
            "final_total": None,
        }
        conf = confirmed.get((g["id"], name))
        if conf:
            try:
                row["confirmed_total"] = float(conf["confirmed_total"])
                row["confirmed_side"] = (conf.get("confirmed_side") or "").upper()
                row["confirmed_price"] = conf.get("confirmed_price") or None
            except (TypeError, ValueError):
                pass
        doc["records"].append(row)
        logged.add(name)
        log(f"[Yen] checkpoint {name} ({timing}) {g['away']} @ {g['home']} "
            f"proj {row['proj']:.1f} line {row['line_total']} status {row['line_status']} "
            f"model {row['model']} issues={','.join(issues) or 'none'}")
    gs["checkpoints"] = sorted(logged)
    save_checkpoints(sd, doc)
    save_state(st)


def maybe_buyout(g, st, tg_token, channel):
    """One advisory buyout per posted game. Uses the original number, not a new odds call."""
    if g.get("state") != "in_progress":
        return
    gs = st["games"].get(g["id"]) or {}
    if not gs.get("posted") or gs.get("buyout_posted"):
        return
    if g.get("period") not in (4, 5) and not g.get("in_ot"):
        return
    pre = st.get("pregame", {}).get(game_key(g["away"], g["home"]))
    line = {"total": gs["posted_line"], "over": None, "under": None}
    res = yen_and_frank(g, line, pre)
    p_side = res["p_over"] if gs["posted_side"] == "OVER" else res["p_under"]
    if not model.buyout_triggered(gs["posted_side"], res["proj"], gs["posted_line"], p_side):
        return
    msg = dell_buyout(g, gs, res["proj"], p_side)
    if dell_post(msg, tg_token, channel):
        gs["buyout_posted"] = True
        log(f"[Livingston Dell] TAKE THE BUYOUT for {g['away']} @ {g['home']} "
            f"(was {gs['posted_side']} {fmt_num(gs['posted_line'])}, proj now {res['proj']:.1f})"
            f"{' (dry run)' if DRY_RUN else ''}")
    else:
        log(f"[Livingston Dell] FAILED to post buyout for {g['away']} @ {g['home']}")
    save_state(st)


def grade_slate(games, sd, st):
    doc = load_checkpoints(sd)
    finals = {}
    for g in games:
        if g.get("state") == "final":
            finals[g["id"]] = g["total"]
    if not finals:
        return
    changed = False
    for rec in doc["records"]:
        if rec.get("final_total") is None and rec.get("game_id") in finals:
            rec["final_total"] = finals[rec["game_id"]]
            rec["grade"] = model.grade_ou(rec["final_total"], rec.get("line_total"), rec.get("model_side"))
            changed = True
    if not changed and not any(r.get("final_total") is not None for r in doc["records"]):
        return
    if changed:
        save_checkpoints(sd, doc)
    summary = {
        "slate": sd.isoformat(),
        "calibration": model.CALIBRATION,
        "baseline": {
            "ppp": model.LEAGUE_PPP,
            "poss_per_team": model.LEAGUE_POSS_PER_TEAM,
            "sample_games": model.LEAGUE_SAMPLE_GAMES,
            "season": model.LEAGUE_SEASON,
            "interval": "UNCALIBRATED — full-game PPP dispersion, not checkpoint forecast errors",
        },
        "by_checkpoint": model.summarize_checkpoints(doc["records"]),
    }
    _write_json(grades_path(sd), summary)
    parts = []
    for name, row in sorted(summary["by_checkpoint"].items()):
        mae = f"{row['mae']:.1f}" if row["mae"] is not None else "n/a"
        cov = f"{row['interval_coverage']:.0%}" if row["interval_coverage"] is not None else "n/a"
        hit = f"{row['ou_hit_rate']:.0%}" if row["ou_hit_rate"] is not None else "n/a"
        parts.append(f"{name} n={row['n']} MAE={mae} cover={cov} hit={hit} pushes={row['ou_pushes']}")
    signature = "; ".join(parts)
    if signature and st.get("grade_sig") != signature:
        st["grade_sig"] = signature
        save_state(st)
        log("[Yen] grade " + signature + " | probabilities remain UNCALIBRATED")


def maybe_attach_box_scores(games, bdl_key, st, sd):
    """ESPN public box scores for live possessions. BALLDONTLIE's free tier has no box."""
    del bdl_key  # scoreboard still uses the BALLDONTLIE key; box stats do not
    if os.environ.get("NBA_SKIP_BOX_SCORES") == "1":
        return
    before = json.dumps(st.get("espn") or {}, sort_keys=True)
    for line in espn_box.attach_espn_boxes(games, st, sd):
        log(line)
    if json.dumps(st.get("espn") or {}, sort_keys=True) != before:
        save_state(st)


def tip_et(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%-I:%M %p ET")
    except Exception:
        return str(iso)


def tip_dt(g):
    tip = g.get("tip")
    if not tip:
        return None
    try:
        return datetime.fromisoformat(tip.replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return None


def slate_should_keep_polling(games, now):
    """Noon job stays up for a live game or one that tips before the evening handoff."""
    cutoff = now.replace(hour=SLATE_HANDOFF[0], minute=SLATE_HANDOFF[1], second=0, microsecond=0)
    for g in games:
        if g["state"] in FINAL_STATES:
            continue
        if g["state"] == "in_progress":
            return True
        tip = tip_dt(g)
        if tip is None or tip <= cutoff:
            return True
    return False


def max_runtime_s():
    raw = (os.environ.get("NBA_MAX_RUNTIME_S") or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def max_polls():
    raw = (os.environ.get("NBA_MAX_POLLS") or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def runtime_exceeded(started):
    limit = max_runtime_s()
    return bool(limit) and (time.monotonic() - started) >= limit


def run_parlay_slot(board):
    """One board, then exit, so this job does not hold the runner."""
    if not DRY_RUN:
        tg_token = need_env("TELEGRAM_BOT_TOKEN")
        channel = need_env("TELEGRAM_CHANNEL_ID")
        odds_key = need_env("ODDS_API_KEY") if board == "final" else ""
    else:
        tg_token = channel = ""
        odds_key = os.environ.get("ODDS_API_KEY", "") if board == "final" else ""
    sd = slate_date()
    log(f"[Danny Ocean] parlay board={board} slate={sd} mode={'dry-run' if DRY_RUN else 'LIVE'}")
    st = load_state(sd)
    post_parlay_board(board, st, sd, tg_token, channel, odds_key)


def run(args):
    slot = slot_name()
    if not slot_hour_ok(slot):
        log(f"[Danny Ocean] slot={slot} is not due at {now_et():%Y-%m-%d %H:%M %Z} -- no-op")
        return
    if slot in ("parlay-early", "parlay-final"):
        run_parlay_slot("early" if slot == "parlay-early" else "final")
        return
    bdl_key = need_env("BALLDONTLIE_API_KEY")
    if args.slate_preview:
        args.check = True
    if not args.check:
        tg_token = need_env("TELEGRAM_BOT_TOKEN") if not DRY_RUN else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        odds_key = need_env("ODDS_API_KEY")
        channel = need_env("TELEGRAM_CHANNEL_ID")
    else:
        tg_token = channel = odds_key = ""
    sd = slate_date()
    log(f"[Danny Ocean] watcher start pid={os.getpid()} slate={sd} slot={slot} mode="
        f"{'check' if args.check else ('dry-run' if DRY_RUN else 'LIVE')}")
    socket.setdefaulttimeout(20)
    st = load_state(sd)
    first = True
    last_status = 0
    polls = 0
    started = time.monotonic()
    looping = not args.check
    if looping:
        clear_done(sd)
        write_heartbeat(sd)
    while True:
        now = now_et()
        if handoff_due(slot, now):
            log(f"[Danny Ocean] slot={slot} handing off at {now:%H:%M %Z} -- state saved")
            save_state(st)
            break
        if runtime_exceeded(started):
            log("[Danny Ocean] max runtime reached -- handing off with state saved")
            save_state(st)
            break
        if now.date() > sd and now.hour >= 3:
            log("[Danny Ocean] past 3:00 AM ET -- shutting down for the night")
            write_done(sd)
            break
        if looping:
            write_heartbeat(sd)
        games = rusty_poll(bdl_key, sd)
        if games is None:
            time.sleep(POLL_LIVE_S)
            continue
        if first:
            log(f"[Rusty Ryan] slate {sd}: {len(games)} games")
            for g in sorted(games, key=lambda x: x["tip"] or ""):
                log(f"[Rusty Ryan]   {g['away']} @ {g['home']} tip {tip_et(g['tip'])} state={g['state']}")
            first = False
        if not games:
            log("[Danny Ocean] no games on the slate -- exiting")
            if looping:
                write_done(sd)
            break
        if args.slate_preview:
            print(dell_slate_text(games, st.get("pregame", {}), sd))
            break
        if args.check:
            for g in games:
                log(f"[Rusty Ryan] {g['away']} @ {g['home']}: state={g['state']} period={g['period']} "
                    f"time={g['raw_time']!r} clock_secs={g['clock_secs']} in_window={g['in_window']}")
            break
        now_ts = time.time()
        if now_ts - last_status >= 600:
            bits = []
            for g in sorted(games, key=lambda x: x["tip"] or ""):
                if g["clock_secs"] is not None:
                    clk = f"Q{g['period']} {fmt_clock(g['clock_secs'])}"
                else:
                    clk = (g["raw_time"] or "-")
                bits.append(f"{g['away_nick']}@{g['home_nick']} {g['state']} {clk}")
            log("[Danny Ocean] status: " + "; ".join(bits))
            last_status = now_ts
        reuben_pregame(st, odds_key, games)           # one Odds API call per slate, totals + spreads
        dell_post_slate(st, games, sd, tg_token, channel)  # reuses that snapshot, no extra credits
        if slot == "slate":
            maybe_final_board_from_slate(st, sd, tg_token, channel, odds_key)
        maybe_attach_box_scores(games, bdl_key, st, sd)
        live = [g for g in games if g["state"] == "in_progress"]
        for g in live:
            window = alert_window(g)
            if window and g["id"] not in st["games"]:
                log(f"[Rusty Ryan] TRIGGER {window} {g['away']} @ {g['home']} Q{g['period']} "
                    f"{fmt_clock(g['clock_secs'])} score {g['away_score']}-{g['home_score']} "
                    f"pace {g['pace_ppm']:.2f} ppm ({g['elapsed_min']:.1f} min elapsed)")
        due = [g for g in live if alert_window(g) and due_for_eval(st["games"].get(g["id"], {}), time.time())]
        lines = None
        if due:
            lines = reuben_fetch(st, odds_key, "live: " + ", ".join(f"{g['away']} @ {g['home']}" for g in due))
            if lines is None:
                for g in due:  # quota skip / API failure: don't burn the game's eval slot
                    log(f"[Frank Catton] PASS / NO PLAY | {g['away']} @ {g['home']} | odds unavailable (quota/API)")
            else:
                for g in due:
                    evaluate(g, lines.get(game_key(g["away"], g["home"])), st, tg_token, channel)
        # Checkpoints reuse last_lines / this poll's fetch. They do not add an Odds API call.
        for g in games:
            if g["state"] in ("in_progress", "final") or g.get("at_halftime") or g.get("at_end_q3"):
                record_checkpoints(g, st, sd, lines)
        for g in live:
            maybe_buyout(g, st, tg_token, channel)
        grade_slate(games, sd, st)
        refresh_picks_record(st, games)
        grade_open_parlays(games)
        polls += 1
        if all(g["state"] in FINAL_STATES for g in games):
            log(f"[Danny Ocean] all {len(games)} games final -- done for the night "
                f"(odds calls {st['odds_calls']}, credits left {st.get('credits_remaining')})")
            write_done(sd)
            break
        if slot == "slate" and not slate_should_keep_polling(games, now):
            log("[Danny Ocean] no afternoon games left to follow -- exiting until the evening run")
            save_state(st)
            break
        limit = max_polls()
        if limit and polls >= limit:
            log(f"[Danny Ocean] reached poll limit ({limit}) -- exiting")
            save_state(st)
            break
        write_heartbeat(sd)
        time.sleep(POLL_LIVE_S if live else POLL_IDLE_S)


# ---- simulate ----------------------------------------------------------------
def _sim_box(away_pts, home_pts, fga_a=78, fga_h=76):
    return {
        "away": {"fga": fga_a, "fta": 22, "oreb": 10, "tov": 12, "pts": away_pts},
        "home": {"fga": fga_h, "fta": 20, "oreb": 9, "tov": 11, "pts": home_pts},
    }


def simulate():
    global DRY_RUN
    DRY_RUN = True
    print("=== SIMULATION: fabricated games, no Odds API / Telegram / BALLDONTLIE calls ===\n")
    print("Clock parser checks:", {t: parse_clock(t) for t in ["6:42", "Q3 6:42", "0:53.4", "53.4", "Halftime", "Final", " ", None]})
    identity = model.project_equal_remaining(185, 10, 1.10, 1.10)
    print(f"Possession identity: 185 + 10 x (1.10 + 1.10) = {identity:.0f}")
    fake = [
        # Q4 6:30, possession model, high pace -> OVER
        ({"id": 1, "visitor_team": {"full_name": "New York Knicks"}, "home_team": {"full_name": "Toronto Raptors"},
          "visitor_team_score": 110, "home_team_score": 104, "period": 4, "time": "6:30",
          "status": "4th Qtr", "status_state": "in_progress", "datetime": "2026-11-18T00:30:00Z",
          "visitor_box": _sim_box(110, 104)["away"], "home_box": _sim_box(110, 104)["home"]},
         {"total": 229.5, "over": -110, "under": -110}, 226.5),
        # Q4 6:00, close game whose projection is UNDER -> Frank PASS
        ({"id": 2, "visitor_team": {"full_name": "Detroit Pistons"}, "home_team": {"full_name": "San Antonio Spurs"},
          "visitor_team_score": 90, "home_team_score": 94, "period": 4, "time": "6:00",
          "status": "4th Qtr", "status_state": "in_progress", "datetime": "2026-11-18T01:00:00Z"},
         {"total": 230.5, "over": -112, "under": -108}, 222.5),
        # Q4 6:00 blowout, projection UNDER -> play (Frank only blocks OVER in a blowout)
        ({"id": 3, "visitor_team": {"full_name": "Utah Jazz"}, "home_team": {"full_name": "LA Clippers"},
          "visitor_team_score": 70, "home_team_score": 100, "period": 4, "time": "6:00",
          "status": "4th Qtr", "status_state": "in_progress", "datetime": "2026-11-18T03:30:00Z"},
         {"total": 220.5, "over": -105, "under": -115}, 221.5),
        # Q4 9:30 is outside the 8:00-5:00 testing window
        ({"id": 4, "visitor_team": {"full_name": "Portland Trail Blazers"}, "home_team": {"full_name": "Minnesota Timberwolves"},
          "visitor_team_score": 90, "home_team_score": 92, "period": 4, "time": "9:30",
          "status": "4th Qtr", "status_state": "in_progress", "datetime": "2026-11-18T01:00:00Z"},
         None, None),
    ]
    sim_games = [scout(dict(r, status_state="scheduled", period=0, time=None,
                            visitor_box=None, home_box=None)) for r, _, _ in fake]
    sim_pre = {game_key(g["away"], g["home"]): p for g, (_, _, p) in zip(sim_games, fake) if p is not None}
    print("\n## Slate card (Livingston Dell):")
    seeded = recordbook.empty_record()
    recordbook.ensure_seeds(seeded)
    recordbook.grade_ready(seeded, {})
    dell_post(dell_slate_text(sim_games, sim_pre, date(2026, 11, 18), seeded), "", "")
    posted = None
    for raw, line, pre in fake:
        g = scout(raw)
        print(f"\n## {g['away']} @ {g['home']}  Q{g['period']} {raw['time']!r} -> clock {g['clock_secs']}s, "
              f"in_window={g['in_window']} model_inputs={'box' if g.get('box') else 'pace fallback'}")
        names = checkpoint_names(g, {})
        if names:
            print(f"   checkpoint: {', '.join(n for n, _ in names)}")
        if not g["in_window"]:
            print("   Rusty: not in trigger window -> Reuben is NOT called")
            continue
        r = yen_and_frank(g, line, pre)
        print(f"   Rusty: score {g['away_score']}-{g['home_score']} (total {g['total']}), "
              f"elapsed {g['elapsed_min']:.2f} min, remaining {g['remaining_min']:.2f} min")
        print(f"   Reuben: FanDuel {line['total']} (O {am(line['over'])}/U {am(line['under'])}), pregame {pre}")
        print(f"   Yen: {r['model']} {r['calibration']} settlement={r['settlement']} "
              f"reg {r['proj_regulation']:.1f} + OT {r['expected_ot_points']:.1f} "
              f"-> {r['proj']:.1f} 80% {r['lo_80']:.1f}–{r['hi_80']:.1f} "
              f"edge {r['edge']:+.1f} P(OVER)={r['p_over']:.2f} P(UNDER)={r['p_under']:.2f} P(push)={r['p_push']:.2f}")
        print(f"   missing: {', '.join(r['missing_inputs'])}")
        print(f"   Frank: blowout={r['blowout']} close={r['close']} -> {r['verdict']} {r['reason']}")
        print(f"   Frank's note: {r['note']}")
        if r["verdict"] in ("OVER", "UNDER"):
            dell_post(dell_format(g, line, r, pre), "", "")
            if posted is None and r["verdict"] == "OVER":
                posted = (g, line, r)
    if posted:
        g, line, r = posted
        cold = scout({
            "id": 1, "visitor_team": {"full_name": g["away"]}, "home_team": {"full_name": g["home"]},
            "visitor_team_score": g["away_score"], "home_team_score": g["home_score"],
            "period": 4, "time": "1:00", "status": "4th Qtr", "status_state": "in_progress",
            "datetime": "2026-11-18T00:30:00Z",
        })
        gs = {"posted": True, "posted_side": "OVER", "posted_line": line["total"], "buyout_posted": False}
        later = yen_and_frank(cold, {"total": line["total"], "over": line["over"], "under": line["under"]}, 226.5)
        print(f"\n## Buyout check at Q4 1:00, score unchanged {cold['away_score']}-{cold['home_score']}, "
              f"proj {later['proj']:.1f} vs {line['total']}")
        if model.buyout_triggered("OVER", later["proj"], line["total"], later["p_over"]):
            dell_post(dell_buyout(cold, gs, later["proj"], later["p_over"]), "", "")
        else:
            print("   buyout threshold not met")
    # Integer-line push, shown so the rule is visible in the simulate log.
    print(f"\n## Integer line: final 210 vs 210 -> {model.grade_ou(210, 210, 'OVER')}; "
          f"211 vs 210.5 OVER -> {model.grade_ou(211, 210.5, 'OVER')}")


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--slate-preview", action="store_true",
                    help="print tonight's slate card using stored pregame totals; no Odds API/Telegram")
    ap.add_argument("--grade", action="store_true",
                    help="print the checkpoint grade summary already stored for a slate; no network")
    ap.add_argument("--parlays", nargs="?", const="final", choices=("early", "final"),
                    help="print the early or final board and do not post or buy props")
    a = ap.parse_args()
    if a.simulate:
        return simulate()
    if a.parlays:
        DRY_RUN = True
        sd = slate_date()
        st = load_state(sd)
        built = parlays.build_for_slate(
            sd, st.get("pregame") or {}, board=a.parlays, prop_quotes=st.get("prop_quotes") or None)
        document = load_parlays()
        early = [row for row in document["parlays"]
                 if row.get("slate") == sd.isoformat() and row.get("board") == "early"]
        changes = parlays.change_note(early, built or []) if a.parlays == "final" else None
        messages = parlays.render_parlays(built or [], a.parlays, changes)
        if not messages:
            print("No parlay cleared the floor.")
            return
        for text in messages:
            print(text)
            print("-" * 40)
        return
    if a.grade:
        sd = slate_date()
        path = grades_path(sd)
        if not os.path.exists(path):
            print(f"no grades file at {path}")
            return
        print(open(path).read())
        return
    DRY_RUN = a.dry_run
    run(a)


if __name__ == "__main__":
    main()
