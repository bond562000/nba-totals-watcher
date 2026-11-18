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

On GitHub Actions, NBA_SLOT is slate | evening | late | manual. A scheduled slot
exits immediately when the current America/New_York hour is not that slot's hour
(UTC cron fires twice, once per DST offset). State is read and written under
NBA_STATE_DIR so a later job on the same slate can resume.
"""
import argparse, html, json, os, re, socket, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BDL_URL = "https://api.balldontlie.io/v1/games"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"

# ---- tunables -------------------------------------------------------------
POLL_LIVE_S = 60          # BALLDONTLIE free tier ~5 req/min -> never faster than 60s
POLL_IDLE_S = 180         # nothing in progress yet
BASELINE_PPM = 4.75       # combined pts/min (~228 / 48)
PACE_WEIGHT = 0.6         # blended = 0.6*pace + 0.4*baseline
MIN_EDGE = 3.5
BLOWOUT_MARGIN = 20
BLOWOUT_RATE_MULT = 0.93
CLOSE_MARGIN = 6
CLOSE_FOUL_BUMP = 3.0
Q3_TRIGGER_SECS = 8 * 60  # Q3 with <= 8:00 left
Q4_MIN_SECS = 9 * 60      # or Q4 with >= 9:00 left
MAX_EVALS_PER_GAME = 2
REEVAL_GAP_S = 6 * 60
MAX_ODDS_CALLS_PER_NIGHT = 15
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
QUOTA_KEYS = ("pregame", "pregame_done", "odds_calls", "credits_remaining")


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
    s = {"id": str(g["id"]), "away": away, "home": home,
         "away_nick": g["visitor_team"].get("name") or away.split()[-1],
         "home_nick": g["home_team"].get("name") or home.split()[-1], "away_score": a, "home_score": h,
         "total": a + h, "margin": abs(a - h), "period": period, "clock_secs": clock,
         "raw_time": t, "status": g.get("status"), "state": state,
         "tip": g.get("datetime"), "in_window": False}
    if state == "in_progress" and clock is not None and period in (1, 2, 3, 4):
        elapsed = (period - 1) * 12 + (12 - clock / 60.0)
        s["elapsed_min"] = elapsed
        s["remaining_min"] = max(0.0, 48 - elapsed)
        s["pace_ppm"] = s["total"] / elapsed if elapsed > 0 else 0.0
        s["in_window"] = (period == 3 and clock <= Q3_TRIGGER_SECS) or \
                         (period == 4 and clock >= Q4_MIN_SECS)
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
    q = urllib.parse.urlencode({"apiKey": odds_key, "regions": "us", "markets": "totals",
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
    log(f"[Reuben Tishkoff] QUOTA: Odds API call #{st['odds_calls']} tonight ({why}); "
        f"x-requests-remaining={hl.get('x-requests-remaining')} used={hl.get('x-requests-used')}")
    save_state(st)
    lines = {}
    for ev in data:
        for bk in ev.get("bookmakers", []):
            if bk.get("key") != "fanduel":
                continue
            for mk in bk.get("markets", []):
                if mk.get("key") != "totals":
                    continue
                ln = {"commence": ev.get("commence_time"), "last_update": mk.get("last_update")}
                for o in mk.get("outcomes", []):
                    if o.get("name") == "Over":
                        ln["total"], ln["over"] = o.get("point"), o.get("price")
                    elif o.get("name") == "Under":
                        ln["under"] = o.get("price")
                        ln.setdefault("total", o.get("point"))
                if ln.get("total") is not None:
                    lines[game_key(ev["away_team"], ev["home_team"])] = ln
    return lines


def reuben_pregame(st, odds_key, games):
    """ONE pregame call per night to record opening/pregame FanDuel totals."""
    if st.get("pregame_done"):
        n = len(st.get("pregame") or {})
        # Once per process. This used to print on every poll tick.
        log_once("pregame-reuse",
                 f"[Reuben Tishkoff] reusing saved pregame totals for {n} games; no Odds API call")
        return
    pending = [g for g in games if g["state"] == "scheduled"]
    if not pending:
        st["pregame_done"] = True
        save_state(st)
        log("[Reuben Tishkoff] pregame snapshot skipped -- no games still scheduled")
        return
    lines = reuben_fetch(st, odds_key, "pregame snapshot")
    st["pregame_done"] = True
    if lines:
        for g in pending:
            k = game_key(g["away"], g["home"])
            if k in lines:
                st["pregame"][k] = lines[k]["total"]
                log(f"[Reuben Tishkoff] pregame FanDuel total {g['away']} @ {g['home']}: {lines[k]['total']}")
            else:
                log(f"[Reuben Tishkoff] pregame: no FanDuel total yet for {g['away']} @ {g['home']}")
    save_state(st)


# =============================================================================
# AGENT 3 -- YEN: live totals quant;  AGENT 4 -- FRANK CATTON: risk + verdict
# =============================================================================
def yen_and_frank(g, line):
    pace = g["pace_ppm"]
    rate = PACE_WEIGHT * pace + (1 - PACE_WEIGHT) * BASELINE_PPM
    rem = g["remaining_min"]
    raw_proj = g["total"] + rem * rate
    # Frank's inside angle
    blowout = g["margin"] >= BLOWOUT_MARGIN
    close = g["margin"] <= CLOSE_MARGIN
    adj_rate = rate * (BLOWOUT_RATE_MULT if blowout else 1.0)
    proj = g["total"] + rem * adj_rate + (CLOSE_FOUL_BUMP if close else 0.0)
    edge = proj - line["total"]
    side = "OVER" if edge > 0 else "UNDER"
    leader = g["home"] if g["home_score"] > g["away_score"] else g["away"]
    verdict, reason = side, ""
    if abs(edge) < MIN_EDGE:
        verdict, reason = "PASS / NO PLAY", f"edge {edge:+.1f} < {MIN_EDGE}"
    elif blowout and side == "OVER":
        verdict, reason = "PASS / NO PLAY", "blowout contradicts OVER"
    elif close and side == "UNDER":
        verdict, reason = "PASS / NO PLAY", "close-game fouling contradicts UNDER"
    if blowout:
        note = (f"{leader} up {g['margin']} late — expect benches to empty in the 4th, "
                f"so we trimmed the rest-of-game scoring rate {int(round((1-BLOWOUT_RATE_MULT)*100))}%.")
    elif close:
        note = (f"Only a {g['margin']}-point game — a tight finish brings intentional fouls and free throws, "
                f"so we added ~{CLOSE_FOUL_BUMP:.0f} points to the projection.")
    else:
        note = (f"{g['margin']}-point margin with no blowout or foul-up risk — "
                f"pace of {pace:.2f} pts/min should hold into the 4th.")
    return {"pace": pace, "rate": rate, "adj_rate": adj_rate, "raw_proj": raw_proj,
            "proj": proj, "edge": edge, "blowout": blowout, "close": close,
            "verdict": verdict, "reason": reason, "note": note}


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


def dell_format(g, line, res, pregame_total=None):
    e = html.escape
    qtxt = f"Q{g['period']} - {fmt_clock(g['clock_secs'])} left"
    move = ""
    if pregame_total is not None:
        d = line["total"] - pregame_total
        move = f" (opened {fmt_num(pregame_total)}, {d:+g})"
    odds = line["over"] if res["verdict"] == "OVER" else line["under"]
    return (f"🏀 {e(g['away'])} @ {e(g['home'])} | {qtxt} | Score: {g['away_score']}-{g['home_score']}\n"
            f"📊 FanDuel Live Line: Total {fmt_num(line['total'])} (Over {am(line['over'])} / Under {am(line['under'])}){move}\n"
            f"🤖 Yen's Projected Total: {res['proj']:.1f}\n"
            f"✅ FINAL CALL: <b>{res['verdict']}</b> ({am(odds)})\n"
            f"📝 Frank's Inside Note: {e(res['note'])}")


def dell_slate_text(games, pregame, sd):
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
    return (f"🏀 {when} NBA Slate ({sd:%b} {sd.day})\n\n" + "\n".join(rows) +
            "\n\nLive Over/Under picks drop here once games hit the 3rd quarter 👀")


def dell_post_slate(st, games, sd, tg_token, channel):
    """Post the slate once per slate date (state flag slate_posted)."""
    if st.get("slate_posted") or not games:
        return
    text = dell_slate_text(games, st.get("pregame", {}), sd)
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
    res = yen_and_frank(g, line)
    pre = st["pregame"].get(game_key(g["away"], g["home"]))
    mv = f" move {line['total'] - pre:+g} (pregame {pre})" if pre is not None else " move n/a"
    log(f"[Yen] {tag} | pace {res['pace']:.2f} ppm, blended {res['rate']:.2f}, adj {res['adj_rate']:.2f}, "
        f"{g['remaining_min']:.1f} min left -> proj {res['proj']:.1f} vs FanDuel {line['total']} "
        f"(O {am(line['over'])}/U {am(line['under'])}){mv} edge {res['edge']:+.1f}")
    log(f"[Frank Catton] {res['verdict']} | {tag} | blowout={res['blowout']} close={res['close']} "
        f"{res['reason']} | eval {gs['evals']}/{MAX_EVALS_PER_GAME}")
    gs.setdefault("decisions", []).append({"at": now_et().isoformat(), "verdict": res["verdict"],
                                           "proj": round(res["proj"], 1), "line": line["total"],
                                           "edge": round(res["edge"], 1)})
    if res["verdict"] in ("OVER", "UNDER"):
        msg = dell_format(g, line, res, pre)
        if dell_post(msg, tg_token, channel):
            gs["posted"] = True
            log(f"[Livingston Dell] posted {res['verdict']} for {g['away']} @ {g['home']}"
                f"{' (dry run)' if DRY_RUN else ''}")
        else:
            log(f"[Livingston Dell] FAILED to post {res['verdict']} for {g['away']} @ {g['home']}")
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


def run(args):
    slot = slot_name()
    if not slot_hour_ok(slot):
        log(f"[Danny Ocean] slot={slot} is not due at {now_et():%Y-%m-%d %H:%M %Z} -- no-op")
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
        reuben_pregame(st, odds_key, games)           # one Odds API call per night, at startup
        dell_post_slate(st, games, sd, tg_token, channel)  # reuses that snapshot, no extra credits
        live = [g for g in games if g["state"] == "in_progress"]
        for g in live:
            if g["in_window"] and g["id"] not in st["games"]:
                log(f"[Rusty Ryan] TRIGGER {g['away']} @ {g['home']} Q{g['period']} "
                    f"{fmt_clock(g['clock_secs'])} score {g['away_score']}-{g['home_score']} "
                    f"pace {g['pace_ppm']:.2f} ppm ({g['elapsed_min']:.1f} min elapsed)")
        due = [g for g in live if g["in_window"] and due_for_eval(st["games"].get(g["id"], {}), time.time())]
        if due:
            lines = reuben_fetch(st, odds_key, "live: " + ", ".join(f"{g['away']} @ {g['home']}" for g in due))
            if lines is None:
                for g in due:  # quota skip / API failure: don't burn the game's eval slot
                    log(f"[Frank Catton] PASS / NO PLAY | {g['away']} @ {g['home']} | odds unavailable (quota/API)")
            else:
                for g in due:
                    evaluate(g, lines.get(game_key(g["away"], g["home"])), st, tg_token, channel)
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
def simulate():
    global DRY_RUN
    DRY_RUN = True
    print("=== SIMULATION: fabricated games, no Odds API / Telegram / BALLDONTLIE calls ===\n")
    print("Clock parser checks:", {t: parse_clock(t) for t in ["6:42", "Q3 6:42", "0:53.4", "53.4", "Halftime", "Final", " ", None]})
    fake = [
        # fast-paced, mid margin -> OVER
        ({"id": 1, "visitor_team": {"full_name": "New York Knicks"}, "home_team": {"full_name": "Toronto Raptors"},
          "visitor_team_score": 74, "home_team_score": 70, "period": 3, "time": "6:42",
          "status": "3rd Qtr", "status_state": "in_progress", "datetime": "2026-11-18T00:30:00Z"},
         {"total": 229.5, "over": -110, "under": -110}, 226.5),
        # slow, close -> UNDER side contradicted by foul risk -> PASS
        ({"id": 2, "visitor_team": {"full_name": "Detroit Pistons"}, "home_team": {"full_name": "San Antonio Spurs"},
          "visitor_team_score": 66, "home_team_score": 63, "period": 3, "time": "Q3 4:10",
          "status": "3rd Qtr", "status_state": "in_progress", "datetime": "2026-11-18T01:00:00Z"},
         {"total": 214.5, "over": -112, "under": -108}, 222.5),
        # slow blowout -> UNDER
        ({"id": 3, "visitor_team": {"full_name": "Utah Jazz"}, "home_team": {"full_name": "LA Clippers"},
          "visitor_team_score": 58, "home_team_score": 84, "period": 3, "time": "2:15",
          "status": "3rd Qtr", "status_state": "in_progress", "datetime": "2026-11-18T03:30:00Z"},
         {"total": 207.5, "over": -105, "under": -115}, 221.5),
        # not yet in window (Q3 9:30)
        ({"id": 4, "visitor_team": {"full_name": "Portland Trail Blazers"}, "home_team": {"full_name": "Minnesota Timberwolves"},
          "visitor_team_score": 60, "home_team_score": 62, "period": 3, "time": "9:30",
          "status": "3rd Qtr", "status_state": "in_progress", "datetime": "2026-11-18T01:00:00Z"},
         None, None),
    ]
    sim_games = [scout(dict(r, status_state="scheduled", period=0, time=None)) for r, _, _ in fake]
    sim_pre = {game_key(g["away"], g["home"]): p for g, (_, _, p) in zip(sim_games, fake) if p is not None}
    print("\n## Slate card (Livingston Dell):")
    dell_post(dell_slate_text(sim_games, sim_pre, date(2026, 11, 18)), "", "")
    for raw, line, pre in fake:
        g = scout(raw)
        print(f"\n## {g['away']} @ {g['home']}  Q{g['period']} {raw['time']!r} -> clock {g['clock_secs']}s, "
              f"in_window={g['in_window']}")
        if not g["in_window"]:
            print("   Rusty: not in trigger window -> Reuben is NOT called")
            continue
        r = yen_and_frank(g, line)
        print(f"   Rusty: score {g['away_score']}-{g['home_score']} (total {g['total']}), elapsed {g['elapsed_min']:.2f} min, "
              f"remaining {g['remaining_min']:.2f} min, pace {r['pace']:.3f} ppm")
        print(f"   Reuben: FanDuel {line['total']} (O {am(line['over'])}/U {am(line['under'])}), pregame {pre}, move {line['total']-pre:+g}")
        print(f"   Yen: blended = 0.6*{r['pace']:.3f} + 0.4*{BASELINE_PPM} = {r['rate']:.3f}; adj rate {r['adj_rate']:.3f}; "
              f"raw proj {r['raw_proj']:.1f}; final proj {r['proj']:.1f}; edge {r['edge']:+.1f}")
        print(f"   Frank: blowout={r['blowout']} close={r['close']} -> {r['verdict']} {r['reason']}")
        print(f"   Frank's note: {r['note']}")
        if r["verdict"] in ("OVER", "UNDER"):
            dell_post(dell_format(g, line, r, pre), "", "")


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--slate-preview", action="store_true",
                    help="print tonight's slate card using stored pregame totals; no Odds API/Telegram")
    a = ap.parse_args()
    if a.simulate:
        return simulate()
    DRY_RUN = a.dry_run
    run(a)


if __name__ == "__main__":
    main()
