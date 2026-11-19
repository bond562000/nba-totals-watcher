"""Possession-based full-game total projection.

This is a hypothesis to evaluate, not a proven edge. Interval widths and
Over/Under probabilities are labeled UNCALIBRATED: the league baseline below
comes from final box scores, and no in-game checkpoint has been backtested.

Possession estimate (NBA Stats glossary, an estimate, not an official count):

    possessions = FGA - OREB + TOV + 0.44 * FTA

An offensive rebound extends the current possession, so OREB is subtracted.
The 0.44 factor is the NBA Stats free-throw possession weight (and-ones and
two-shot fouls are not separated). Both teams' estimates usually differ by a
possession or two.

Checked on ESPN team box scores for the 2023-24 regular season
(sportsdataverse release espn_nba_team_boxscores, team_box_2024.csv,
season_type == 2): 1,232 games, 2,464 team rows.

    mean estimated possessions / team = 100.84 (SD 5.56)
    mean points / estimated possession = 1.1339 (SD 0.1194)
    mean final total = 228.56
    mean |poss_home - poss_away| = 2.26

Those finals have no clock, no quarter splits, no lineup, and no betting line,
so they cannot calibrate a live forecast. The possession-level point SD used
for the 80% interval is inferred by scaling the full-game PPP SD by sqrt(mean
possessions), which assumes i.i.d. possessions. That assumption is not a
validated forecast-error model.
"""
import math

CALIBRATION = "UNCALIBRATED"

# 2023-24 regular season finals. Not a live-forecast fit.
LEAGUE_PPP = 1.1339
LEAGUE_PPP_SD = 0.1194
LEAGUE_POSS_PER_TEAM = 100.84
LEAGUE_SAMPLE_GAMES = 1232
LEAGUE_SAMPLE_TEAM_ROWS = 2464
LEAGUE_SEASON = "2023-24 regular season (ESPN team box scores via sportsdataverse)"

# Play-by-play replay of 18 final games, 2026-11-14 through 2026-11-17.
# Boxes are reconstructed from ESPN plays (not the official team line), with no
# betting line. Coverage is not ~80% at every mark and the sample is short,
# so the interval and the OT add-on stay UNCALIBRATED and were not refit.
ESPN_REPLAY = {
    "label": "UNCALIBRATED",
    "games": 18,
    "dates": "2026-11-14 through 2026-11-17",
    "ot_games": 2,
    "mean_ot_points_when_reached": 5.5,
    "checkpoints": {
        "halftime": {"n": 18, "mae": 13.32, "bias": -4.75, "cover80": 0.61},
        "q3_2:00": {"n": 18, "mae": 6.74, "bias": -0.91, "cover80": 0.78},
        "end_q3": {"n": 18, "mae": 6.85, "bias": -0.43, "cover80": 0.89},
        "q4_8:00": {"n": 18, "mae": 4.91, "bias": -1.24, "cover80": 0.78},
        "q4_6:30": {"n": 18, "mae": 4.28, "bias": -1.10, "cover80": 0.89},
        "q4_5:00": {"n": 18, "mae": 4.27, "bias": -1.66, "cover80": 0.78},
        "q4_2:00": {"n": 18, "mae": 3.74, "bias": -1.61, "cover80": 0.83},
    },
}

# Shrinkage prior strength in possessions. Not fit on checkpoint data.
# 40 possessions (~half of a half) is the point where observed PPP gets half the weight.
PPP_PRIOR_POSSESSIONS = 40
PACE_PRIOR_MINUTES = 12.0
# Recent game-clock window is shrunk toward the full-game rate. 12 possessions
# is about two minutes of action. Not a fitted weight.
RECENT_PRIOR_POSSESSIONS = 12
RECENT_WINDOW_SEC = 180

# Inferred possession-level points SD. See module docstring.
POINT_SD = LEAGUE_PPP_SD * math.sqrt(LEAGUE_POSS_PER_TEAM)
Z_80 = 1.2815515655446004  # N(0,1) 90th percentile; central 80% interval
INTERVAL_SD_FLOOR = 2.5

# Uncalibrated OT add-on so the number can be compared to a FanDuel full-game
# total (which includes overtime). Not fit.
MARGIN_SD_PER_SQRT_MIN = 2.5
OT_POINTS_IF_REACHED = 15.0
REGULATION_MINUTES = 48.0

# Scoring-rate fallback (the previous Yen blend). Used only when no box score.
BASELINE_PPM = 4.75
PACE_WEIGHT = 0.6

# Buyout is advisory. Both conditions are required so a small swing does not fire.
BUYOUT_POINTS = 6.0
BUYOUT_MAX_PROB = 0.35

MISSING_WITHOUT_BOX = (
    "team_box_fga_oreb_tov_fta",
    "play_by_play",
    "recent_window_efficiency",
    "quarter_splits",
    "current_lineup",
    "substitutions",
    "bonus",
    "possession_arrow",
    "timeouts",
    "intentional_fouls",
    "season_team_ratings",
)
MISSING_WITH_BOX = tuple(x for x in MISSING_WITHOUT_BOX if x != "team_box_fga_oreb_tov_fta")

FT_POSSESSION_WEIGHT = 0.44


def estimate_possessions(fga, oreb, tov, fta):
    """NBA Stats box-score possession estimate. Not a counted possession total."""
    return float(fga) - float(oreb) + float(tov) + FT_POSSESSION_WEIGHT * float(fta)


def project_from_remaining(current, rem_poss_a, eppp_a, rem_poss_b, eppp_b):
    """current + remPoss_A * ePPP_A + remPoss_B * ePPP_B."""
    return float(current) + float(rem_poss_a) * float(eppp_a) + float(rem_poss_b) * float(eppp_b)


def project_equal_remaining(current, rem_poss_per_team, eppp_a, eppp_b):
    """Equal remaining possessions: current + remPoss * (ePPP_A + ePPP_B)."""
    return project_from_remaining(current, rem_poss_per_team, eppp_a, rem_poss_per_team, eppp_b)


def blend_rate(observed, observed_n, baseline, prior_n):
    """Shrink `observed` toward `baseline`. Weight is n / (n + prior), not a fitted edge."""
    if observed is None or observed_n is None or observed_n <= 0:
        return float(baseline), 0.0
    w = float(observed_n) / (float(observed_n) + float(prior_n))
    return w * float(observed) + (1.0 - w) * float(baseline), w


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def is_integer_line(line):
    return abs(float(line) - round(float(line))) < 1e-9


def ou_probabilities(proj, line, sd):
    """P(final > line), P(final < line), P(push) under an uncalibrated Normal.

    A push is possible only on an integer line, when the final total equals the
    number. Half-point lines have no push. The probability is not the raw gap.
    """
    sd = max(float(sd), 1e-6)
    proj = float(proj)
    line = float(line)
    if is_integer_line(line):
        p_under = _phi((line - 0.5 - proj) / sd)
        p_over = 1.0 - _phi((line + 0.5 - proj) / sd)
        p_push = max(0.0, 1.0 - p_over - p_under)
        return p_over, p_under, p_push
    p_over = 1.0 - _phi((line - proj) / sd)
    return p_over, 1.0 - p_over, 0.0


def interval_80(proj, rem_poss_a, rem_poss_b):
    """Central 80% interval. Width is UNCALIBRATED (see module docstring)."""
    rem = max(0.0, float(rem_poss_a)) + max(0.0, float(rem_poss_b))
    sd = max(INTERVAL_SD_FLOOR, POINT_SD * math.sqrt(rem if rem > 0 else 1.0))
    half = Z_80 * sd
    return float(proj) - half, float(proj) + half, sd


def expected_ot(margin, remaining_min, in_ot=False):
    """Expected overtime points added to a regulation projection.

    FanDuel totals settle on the full game including OT. P(OT) is the Normal
    density of the current margin, treated as a 1-point bin, with an
    uncalibrated margin SD. Already-in-OT and tied returns the OT-points prior.
    """
    if in_ot:
        if margin == 0:
            return OT_POINTS_IF_REACHED, 1.0
        return 0.0, 0.0
    if remaining_min is None or remaining_min <= 0:
        if margin == 0:
            return OT_POINTS_IF_REACHED, 1.0
        return 0.0, 0.0
    sigma = MARGIN_SD_PER_SQRT_MIN * math.sqrt(float(remaining_min))
    sigma = max(sigma, 1e-6)
    z = float(margin) / sigma
    density = math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))
    p_ot = min(0.35, density)
    return p_ot * OT_POINTS_IF_REACHED, p_ot


def pace_fallback(total, elapsed_min, remaining_min):
    """Previous scoring-rate projection, with no blowout trim and no foul bump."""
    elapsed = float(elapsed_min or 0.0)
    remaining = max(0.0, float(remaining_min or 0.0))
    if elapsed <= 0:
        pace = BASELINE_PPM
    else:
        pace = float(total) / elapsed
    rate = PACE_WEIGHT * pace + (1.0 - PACE_WEIGHT) * BASELINE_PPM
    return float(total) + remaining * rate, pace, rate


def _box_side(box, side):
    if not box:
        return None
    raw = box.get(side)
    if not raw:
        return None
    needed = ("fga", "oreb", "tov", "fta")
    if any(raw.get(k) is None for k in needed):
        return None
    poss = estimate_possessions(raw["fga"], raw["oreb"], raw["tov"], raw["fta"])
    if poss <= 1:
        return None
    pts = raw.get("pts")
    return {"poss": poss, "pts": pts, "ppp": (None if pts is None else float(pts) / poss)}


def project_game(g, pregame_total=None):
    """Full-game (regulation + expected OT) projection for one scouted game.

    `settlement` is always `full_game_including_ot` because that is what a
    FanDuel total grades. The regulation piece is returned separately.
    """
    total = float(g.get("total") or 0)
    elapsed = g.get("elapsed_min")
    remaining = g.get("remaining_min")
    margin = g.get("margin") or 0
    in_ot = bool(g.get("in_ot"))
    away_box = _box_side(g.get("box"), "away")
    home_box = _box_side(g.get("box"), "home")
    if g.get("missing_inputs") is not None:
        missing = list(g["missing_inputs"])
    else:
        missing = list(MISSING_WITH_BOX if (away_box and home_box) else MISSING_WITHOUT_BOX)
    notes = []

    if away_box and home_box and elapsed and elapsed > 0 and remaining is not None:
        eppp_a, w_a = blend_rate(away_box["ppp"], away_box["poss"], LEAGUE_PPP, PPP_PRIOR_POSSESSIONS)
        eppp_b, w_b = blend_rate(home_box["ppp"], home_box["poss"], LEAGUE_PPP, PPP_PRIOR_POSSESSIONS)
        recent_a = _box_side(g.get("recent_box"), "away")
        recent_b = _box_side(g.get("recent_box"), "home")
        # Logged only. An 18-game ESPN replay (2026-11-14 to 2026-11-17) raised
        # MAE at every checkpoint except q4_2:00 when this window was blended in.
        w_ra = w_rb = 0.0
        if recent_a and recent_b and recent_a["ppp"] is not None and recent_b["ppp"] is not None:
            notes.append(
                f"last {RECENT_WINDOW_SEC // 60} min of game clock logged "
                f"(away {recent_a['ppp']:.2f} PPP on {recent_a['poss']:.1f} poss, "
                f"home {recent_b['ppp']:.2f} on {recent_b['poss']:.1f}); not blended")
        if pregame_total:
            exp_full = float(pregame_total) / (2.0 * LEAGUE_PPP)
        else:
            exp_full = LEAGUE_POSS_PER_TEAM
            missing.append("pregame_total")
        pre_rate = exp_full / REGULATION_MINUTES
        # One shared pace. The two box-score estimates are not a possession arrow.
        obs_poss = 0.5 * (away_box["poss"] + home_box["poss"])
        obs_rate = obs_poss / float(elapsed)
        pace_w = float(elapsed) / (float(elapsed) + PACE_PRIOR_MINUTES)
        rate = pace_w * obs_rate + (1.0 - pace_w) * pre_rate
        rem = max(0.0, float(remaining) * rate)
        if abs(away_box["poss"] - home_box["poss"]) > 3:
            notes.append("possession estimates differ by more than 3; remaining possessions still split equally")
        regulation = project_equal_remaining(total, rem, eppp_a, eppp_b)
        model = "possession_estimate"
        poss_a = poss_b = rem
        pace_ppm = (total / float(elapsed)) if elapsed else None
        detail = {
            "eppp_away": eppp_a, "eppp_home": eppp_b,
            "ppp_weight_away": w_a, "ppp_weight_home": w_b,
            "poss_away_est": away_box["poss"], "poss_home_est": home_box["poss"],
            "rem_poss_per_team": rem, "pace_weight": pace_w,
            "recent_weight_away": w_ra, "recent_weight_home": w_rb,
        }
    else:
        if elapsed is None or remaining is None:
            elapsed = elapsed or 0.0
            remaining = remaining or 0.0
        regulation, pace_ppm, rate = pace_fallback(total, elapsed, remaining)
        model = "pace_fallback"
        # Translate the fallback into a possession count only for the interval width.
        poss_a = poss_b = max(0.0, float(remaining or 0.0) * (LEAGUE_POSS_PER_TEAM / REGULATION_MINUTES))
        detail = {"fallback_rate_ppm": rate, "pace_ppm": pace_ppm}
        notes.append("no live possession totals; scoring-rate fallback")

    ot_pts, p_ot = expected_ot(margin, remaining, in_ot=in_ot)
    proj = regulation + ot_pts
    lo, hi, sd = interval_80(proj, poss_a, poss_b)
    return {
        "model": model,
        "calibration": CALIBRATION,
        "settlement": "full_game_including_ot",
        "proj_regulation": regulation,
        "expected_ot_points": ot_pts,
        "p_ot": p_ot,
        "proj": proj,
        "lo_80": lo,
        "hi_80": hi,
        "sd": sd,
        "interval_label": CALIBRATION,
        "prob_label": CALIBRATION,
        "missing_inputs": missing,
        "notes": notes,
        "pace": pace_ppm if pace_ppm is not None else g.get("pace_ppm") or 0.0,
        "detail": detail,
        "baseline_ppp": LEAGUE_PPP,
        "baseline_sample_games": LEAGUE_SAMPLE_GAMES,
    }


def grade_ou(actual, line, side):
    """Win, loss, or push. An integer line pushes when the final total equals it."""
    if actual is None or line is None or side not in ("OVER", "UNDER"):
        return None
    actual = float(actual)
    line = float(line)
    if is_integer_line(line) and actual == round(line):
        return "push"
    if side == "OVER":
        return "win" if actual > line else "loss"
    return "win" if actual < line else "loss"


def covered(actual, lo, hi):
    if actual is None or lo is None or hi is None:
        return None
    return float(lo) <= float(actual) <= float(hi)


def buyout_triggered(side, proj, line, p_side):
    """True when the updated projection has swung hard against the posted call.

    Requires a move of at least BUYOUT_POINTS to the wrong side of the original
    number AND an uncalibrated side-probability under BUYOUT_MAX_PROB.
    """
    if side not in ("OVER", "UNDER") or proj is None or line is None or p_side is None:
        return False
    if side == "OVER":
        swung = (float(line) - float(proj)) >= BUYOUT_POINTS
    else:
        swung = (float(proj) - float(line)) >= BUYOUT_POINTS
    return swung and float(p_side) < BUYOUT_MAX_PROB


def summarize_checkpoints(records):
    """MAE, 80% coverage, and O/U results by checkpoint. Pushes are not hits or misses."""
    buckets = {}
    for rec in records:
        actual = rec.get("final_total")
        if actual is None:
            continue
        name = rec.get("checkpoint") or "unknown"
        b = buckets.setdefault(name, {
            "n": 0, "abs_err": 0.0, "covered": 0, "cover_n": 0,
            "wins": 0, "losses": 0, "pushes": 0,
            "confirmed_wins": 0, "confirmed_losses": 0, "confirmed_pushes": 0,
            "confirmed_n": 0,
        })
        b["n"] += 1
        if rec.get("proj") is not None:
            b["abs_err"] += abs(float(actual) - float(rec["proj"]))
        flag = covered(actual, rec.get("lo_80"), rec.get("hi_80"))
        if flag is not None:
            b["cover_n"] += 1
            b["covered"] += int(flag)
        result = grade_ou(actual, rec.get("line_total"), rec.get("model_side"))
        if result == "win":
            b["wins"] += 1
        elif result == "loss":
            b["losses"] += 1
        elif result == "push":
            b["pushes"] += 1
        if rec.get("confirmed_total") is not None and rec.get("confirmed_side"):
            b["confirmed_n"] += 1
            cresult = grade_ou(actual, rec["confirmed_total"], rec["confirmed_side"])
            if cresult == "win":
                b["confirmed_wins"] += 1
            elif cresult == "loss":
                b["confirmed_losses"] += 1
            elif cresult == "push":
                b["confirmed_pushes"] += 1
    out = {}
    for name, b in buckets.items():
        decided = b["wins"] + b["losses"]
        out[name] = {
            "n": b["n"],
            "mae": (b["abs_err"] / b["n"]) if b["n"] else None,
            "interval_coverage": (b["covered"] / b["cover_n"]) if b["cover_n"] else None,
            "interval_label": CALIBRATION,
            "ou_wins": b["wins"],
            "ou_losses": b["losses"],
            "ou_pushes": b["pushes"],
            "ou_hit_rate": (b["wins"] / decided) if decided else None,
            "confirmed_n": b["confirmed_n"],
            "confirmed_wins": b["confirmed_wins"],
            "confirmed_losses": b["confirmed_losses"],
            "confirmed_pushes": b["confirmed_pushes"],
            "sample_note": "Coverage and hit rate describe logged games only. They do not calibrate the probability model.",
        }
    return out
