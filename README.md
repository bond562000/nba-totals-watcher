# NBA totals watcher

Live NBA Over/Under watcher. It polls [BALLDONTLIE](https://www.balldontlie.io/) for the scoreboard, reads FanDuel totals from [The Odds API](https://the-odds-api.com/), and posts a daily slate plus at most one pick per game to Telegram.

GitHub-hosted jobs stop at 6 hours, and `schedule` is UTC and often late. The workflow runs the watcher three times a night. Each run restores the slate's state, polls, and pushes that state back, so a restart keeps the same pregame line, the same eval counts, and the same "already posted" flags.

The watcher is Python 3.11+ with the standard library only.

## Secrets

Add these four repository secrets. Nothing else is read (no secret files), and the values are never printed.

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Used for |
| --- | --- |
| `BALLDONTLIE_API_KEY` | Scoreboard (`Authorization` header) |
| `TELEGRAM_BOT_TOKEN` | Posting cards |
| `ODDS_API_KEY` | FanDuel totals (The Odds API) |
| `TELEGRAM_CHANNEL_ID` | Channel that receives the cards |

Enable Actions on the repository if this is the first workflow (Actions tab → enable). Cron starts after the workflow file is on `main`. **Run workflow** works as soon as the file is there.

## Manual dry run

1. Add the four secrets above.
2. Open **Actions → NBA totals watcher → Run workflow**.
3. Mode: **dry-run**. Leave **follow** off.
4. Open the job log. Cards appear between `DRY RUN, would post` lines. Telegram is not called.

That pass still calls BALLDONTLIE. It calls The Odds API only when tonight's saved state has no pregame snapshot yet (one credit). A later run on the same slate reuses the saved totals.

| Mode | What it does |
| --- | --- |
| `dry-run` | Full rules, cards printed in the log, no Telegram post. |
| `check` | One BALLDONTLIE poll, then exit. No Odds API, no Telegram. |
| `simulate` | Fabricated games. No network and no secrets. |
| `live` | Posts to Telegram. |

Leave **follow** off for a manual run. One pass then exits, so the button does not hold the runner through the night. **follow** keeps polling until the slate ends (or the 5 hour 50 minute cap) and waits if a scheduled run is already going. Scheduled runs share one concurrency group, `nba-totals-watcher`, so two watchers never poll at the same time. A run that is already polling is left running; a newer waiting run replaces an older waiting run.

## Schedule (Eastern Time)

Each slot is registered twice, an hour apart, because noon ET is 16:05 UTC on EDT and 17:05 UTC on EST. The same split applies to the other slots. `watcher.py` checks `America/New_York` and exits when the hour is wrong, before any API call. A second cron later in the same hour covers a dropped first fire.

| Slot | Starts (ET) | Backup (ET) | Stops | Job |
| --- | --- | --- | --- | --- |
| Slate | 12:05 PM | 12:30 PM | 5:55 PM, or sooner when every remaining tip is after that | Posts the slate. Keeps polling while a game is live or tips before 5:55 PM (weekend afternoons). |
| Evening | 5:45 PM | 6:20 PM | 11:30 PM | Waits if the slate job is still polling, then polls about every 60s while games are live (180s when idle). |
| Late | 11:20 PM | 11:45 PM | 3:00 AM, or when every game is final | Same polling. Covers the end of the night after the evening job hands off, including West Coast games that finish around 1:30 AM. |

The evening cron is 5:45 PM so it is already waiting when the slate job stops at 5:55 PM. A slate job that started at noon cannot still be running at 6:30 PM (the platform stops jobs at 6 hours). The handoff is a few minutes of Actions startup, and state is already on the `state` branch.

Accepted clock, so a late cron still runs and the other DST offset does not:

- Slate: ET hour 12.
- Evening: 5:40–6:40 PM.
- Late: 11:00–11:59 PM, or 12:00–12:15 AM.

On a normal weekday the slate job posts, takes the one pregame Odds API snapshot, sees no afternoon tip, and exits. The evening job reuses that snapshot.

The noon runs on Nov 18 never started. The workflow file reached `main` at 11:14 AM ET, after the 11:05 AM EDT-offset cron and only minutes before GitHub would have had to register the new schedule. The 12:05 and 12:30 PM EST crons are not in the Actions history. GitHub often skips the first scheduled fires after a workflow is added to the default branch; the cron expressions themselves are valid, and a manual run at 3:55 PM ET is what posted the slate. An evening or manual run still posts the slate when `slate_posted` is false, so a missed noon cron does not drop the card. The schedule is unchanged in this update.

## CourtSide record

The slate post opens with the running record of every full OVER/UNDER the watcher has posted, graded on the official final including overtime. Leans are graded too and shown beside that record, so a lean does not change the main win-loss line. A push is shown only when one has happened. Win percentage ignores pushes. The streak skips pushes. Last night is one line. Buyouts do not change the grade, and a second call for the same game is ignored. PASS / NO PLAY is not a result. Confirmed bets from **Record confirmed line** are graded in `record.json` and are not part of the headline.

```
<b>CourtSide Picks (1-2 · 33% · L2)</b> · Leans (0-0)
Last night: ✅ Knicks/Raptors O 210.5 ❌ Blazers/Wolves U 207.5 ❌ Jazz/Clippers O 242.5
🏀 Tonight's NBA Slate (Nov 18)
```

The Nov 17 seeds are in `recordbook.py`, checked against the ESPN scoreboard: Knicks 111–Raptors 102 (213) covered OVER 210.5; Blazers 105–Timberwolves 106 (211) missed UNDER 207.5; Jazz 132–Clippers 107 (239) missed OVER 242.5. The 12:04 AM post fits the Jazz tip at 10:30 PM, not the Hornets tip at 11:00 PM.

## State

Slate state lives on a branch named `state`, as `state-YYYY-MM-DD.json` (the NBA date, which stays on the previous calendar date until 6:00 AM ET). The file holds:

- `slate_posted`
- per-game `posted`, `evals`, and the last eval time
- `odds_calls` and `credits_remaining`
- `pregame` totals and `pregame_done`

The job restores that file at start, the watcher rewrites it as games are evaluated, and a side loop pushes it to `state` about every 30 seconds and again when the job exits. Checkpoint records, grade summaries, and `confirmed-lines.csv` go to the same branch. The next run of the same night sees the same flags, so it does not post the slate twice, does not post a second card for a game, and does not buy a second pregame line.

`--dry-run` writes `state-YYYY-MM-DD-dryrun.json`. When the live file already has the pregame snapshot, the dry run reuses it. When the dry run is the one that takes the snapshot, that snapshot and the call count are copied onto the live file so the night still spends one pregame credit. Dry-run posts do not set the live `slate_posted` or per-game `posted` flags.

This workflow has no `push` trigger, so commits on `state` do not start it. Pushes made with the workflow's `GITHUB_TOKEN` do not start workflows anyway.

## Rules

- Alert window, while the possession model is still a hypothesis: 4th quarter with 8:00 through 5:00 left. The last 2:00 is logged and watched for a buyout, and it does not send a new pick.
- Q4, 8:00 through 5:00: a full play needs an edge of at least 3.5. From 3.0 up to 3.5 the card is a lean (`⚠️ LEAN`), not a full play. Late Q3 (2:00 left through the end of the quarter, and the first minute of Q4) needs 5.0 for a play and 4.5 for a lean, because that part of the game has been noisier.
- Frank passes an OVER when the margin is 22 or more, and an UNDER when the margin is 6 or less. The close-game line stays at 6. The replay measures projection error, not whether a close game's under is a bad bet, so that veto is unchanged. Neither veto changes the projected number.
- One pick per game per night, plus at most one advisory buyout. At most two evaluations per game, at least 6 minutes apart. The testing window is often shorter than that gap, so most games get one evaluation. A quota skip does not use up an evaluation.
- Odds API budget: one pregame call per slate, at most 18 calls a night, and no call when fewer than 25 credits are left. The cap was 15 when the only live window was Q4. Late Q3 adds about one fetch per tip cluster, so three clusters is +3. Checkpoint logs still reuse the last fetch and add **0** credits. A normal night is still about 6–9 credits, not 18. "Reusing saved pregame totals" is logged once per process.

## Possession model (hypothesis)

Regulation projection, when both teams have a box-score estimate:

```
possessions ≈ FGA − OREB + TOV + 0.44 × FTA
ePPP = (n / (n + 40)) × observed PPP + (40 / (n + 40)) × 1.1339
remaining possessions per team = minutes left × blended possessions per minute
projected regulation total = current points + remaining possessions × (ePPP_away + ePPP_home)
```

`185 + 10 × (1.10 + 1.10) = 207` is the equal-remaining form of that sum. An offensive rebound extends the possession, so OREB is subtracted. The 0.44 weight is the [NBA Stats glossary](https://www.nba.com/mavs/advanced-stats-glossary) estimate (and-ones are not split out). The output is labeled an estimate.

The full-game number adds an expected overtime total, because FanDuel grades the whole game including OT. That add-on is an uncalibrated Normal-density guess (margin SD `2.5 × sqrt(minutes left)`, 15 points if OT is reached). Grades use the final score, which includes OT. An integer line is a push when the final total equals the number; half-point lines cannot push.

80% intervals and Over/Under probabilities assume a Normal residual whose width comes from scaling the full-game PPP standard deviation by `sqrt(possessions)`. They are **UNCALIBRATED**. The probability is not the raw point gap.

When the box score is missing, Yen uses the old scoring-rate fallback: `0.6 × live points per minute + 0.4 × 4.75`, plus the same OT add-on, and the card says `pace fallback`.

### What was fit, and what was not

| Piece | Status | Source |
| --- | --- | --- |
| League PPP 1.1339 and 100.84 possessions per team | Fit on finals only | ESPN team box scores, 2023-24 regular season, 1,232 games, via [sportsdataverse](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_team_boxscores) `team_box_2024.csv`, `season_type=2`. Mean final total 228.56. Mean gap between the two teams' possession estimates 2.26. |
| Shrinkage prior of 40 possessions, pace prior of 12 minutes | Not fit | Chosen so a short sample does not dominate. Not a backtest. |
| 80% interval and O/U probabilities | UNCALIBRATED | No in-game snapshots and no historical lines in that file, so coverage has not been checked. Logged grades will report empirical coverage later; the label stays UNCALIBRATED. |
| OT expectation | UNCALIBRATED | No overtime model was fit. |
| Blowout / close-game rate adjustments | Removed from the number | Frank's pass/fail veto is unchanged. |

Replay the PPP fit with `python3 scripts/fit_league_baseline.py path/to/team_box_2024.csv`. The watcher does not download that file during a game.

### Live box scores from ESPN

BALLDONTLIE's free tier is the scoreboard only. Team possessions come from ESPN's public, unauthenticated site API (no key):

- `site.api.espn.com/.../nba/scoreboard?dates=YYYYMMDD` once per slate date per poll, plus the next calendar date only if a game did not match
- `.../nba/summary?event=ID` once per live game, reused for 50 seconds

The team line supplies FGA, offensive rebounds, total turnovers (player plus team), and FTA. Quarter linescores come from the same summary. Play-by-play supplies a 3-minute game-clock window, timeout and intentional-foul counts, a foul-based bonus estimate, and substitution events. One competitor can carry a possession flag. ESPN's `active` athletes are stored, and treated as the on-court five only when the game is in progress and each side has exactly five.

ESPN is unofficial. The stats are used only when the score matches BALLDONTLIE and, while the game is live, the period and clock are within 90 seconds. A mismatch, an empty box, a timeout, or an HTTP 429 keeps the pace fallback and logs the reason. A 429 backs off (two minutes, then longer) instead of hammering the host. No ESPN call is made for a slate that is already final.

On 18 final games from 2026-11-14 through 2026-11-17, replaying the play-by-play to each checkpoint (no betting line; possessions reconstructed from plays) gave:

| Checkpoint | n | MAE | Bias (proj − final) | 80% coverage |
| --- | --- | --- | --- | --- |
| Halftime | 18 | 13.3 | −4.8 | 61% |
| Q3 2:00 | 18 | 6.7 | −0.9 | 78% |
| End of Q3 | 18 | 6.9 | −0.4 | 89% |
| Q4 8:00 | 18 | 4.9 | −1.2 | 78% |
| Q4 6:30 | 18 | 4.3 | −1.1 | 89% |
| Q4 5:00 | 18 | 4.3 | −1.7 | 78% |
| Q4 2:00 | 18 | 3.7 | −1.6 | 83% |

Two of those games reached OT (mean 5.5 extra points). Blending the recent window into efficiency raised MAE at every mark except Q4 2:00, so the window is logged and not applied. The interval, the 40-possession shrinkage, and the OT add-on were not refit. They stay **UNCALIBRATED**. Re-run with `python3 scripts/calibrate_from_espn.py 4 18`.

Still missing after a good ESPN read: season team ratings, official timeouts remaining, and a confirmed on-court lineup when ESPN does not flag exactly five active players per side. Bonus is estimated from foul plays, not an ESPN bonus flag.

The Odds API is not called for a checkpoint. A night is 1 pregame credit plus one live credit per poll in which a game inside 8:00–5:00 is due for an evaluation (one call covers every due game). Cap 15, and no call under 25 credits remaining. A typical night is a handful of live calls, not one per checkpoint. ESPN requests do not spend Odds API credits.

### Checkpoints and the line Richie confirmed

A record is stored at halftime, end of Q3, Q4 8:00, 6:30, 5:00, and Q4 2:00, when the poll is within 70 game-clock seconds of that mark or the clock crossed it since the previous poll. Score and clock have to be consistent. A missing or stale price is stored as missing, not invented. Lineup is always logged as missing on the free tier.

Files on the `state` branch, next to `state-YYYY-MM-DD.json`:

- `checkpoints-YYYY-MM-DD.json` — the model signal and the line that was actually available
- `grades-YYYY-MM-DD.json` — MAE, interval coverage, O/U record by checkpoint (pushes excluded from the hit rate)
- `confirmed-lines.csv` — the number Richie confirmed

Record a confirmed line with **Actions → Record confirmed line → Run workflow**. That job uses a different concurrency group (`nba-totals-records`), so it does not block the live watcher, and it spends no Odds API credits. `confirmed-lines.example.csv` shows the columns.

After games go final, `python3 watcher.py --grade` prints the stored summary. Probabilities in that summary are still UNCALIBRATED.

### Buyout

After an OVER or UNDER is posted, the watcher keeps projecting that game. It sends one `⚠️ TAKE THE BUYOUT` follow-up when the updated projection is at least 6 points through the original number the wrong way and the uncalibrated probability of the original side is under 35%. Both have to be true. FanDuel cash-out values are not in the API, so the message is advisory only.

### Alert

The card is still the five-line quick read. The FanDuel total, Yen's projection, and the final-call number are bold. An OVER/UNDER names the total (`OVER 229.5`). One extra line carries the uncalibrated interval and probability.

## Pregame parlays

The noon run, after the slate card, posts one player-parlay message and one totals-parlay message when any legs clear the floor. If the noon job missed, the next run still posts them while a game is scheduled. `--parlays` prints the same card and does not send it.

Player legs are floors from ESPN game logs: 15+ points, 3+ rebounds, 1+ three, 1+ steal. A leg needs an 85% hit rate over the last 10 and over the season, average minutes of at least 24, and a stable minute load. Against one of the eight stingiest defenses, points and threes need 90%. Out and doubtful players are skipped. The number on the card is that historical rate, labeled UNCALIBRATED.

Totals legs use the FanDuel number already saved in `state.pregame` and a projection from each team's season points scored and allowed, blended 60/40 with the last five finals. The gap has to be at least 3 points. Those legs are separate from the live single-game calls.

A parlay is 4 to 6 legs. The combined probability is the product and assumes independence. If two legs share a game, the card says the product ignores that correlation. After the games end, each leg and each parlay is graded into `parlays.json`. A player leg is graded only from a final box, so a pregame zero does not become a loss. The slate header adds `Parlays (0-0) · Legs (0-0)`.

Player-prop prices stay off. The Odds API serves them only on the single-event odds endpoint, at 1 credit per market per region. Points, rebounds, threes, and steals for one game is 4 credits, about 48 credits on a 12-game slate, which does not fit beside the live totals in a 500-credit month. About 488 credits were left when this was checked.

## Local commands

```bash
python3 watcher.py --simulate
python3 -m unittest discover -s tests -v
```

`--simulate` fabricates four games and prints the slate card, a possession-model OVER, a pace-fallback UNDER, a Frank pass, a checkpoint, and a buyout. It does not touch the network.

```bash
BALLDONTLIE_API_KEY=... python3 watcher.py --check
BALLDONTLIE_API_KEY=... ODDS_API_KEY=... TELEGRAM_CHANNEL_ID=... python3 watcher.py --dry-run
```

`--check` is one scoreboard poll. `--dry-run` follows the live loop and prints cards instead of posting. Point `NBA_STATE_DIR` at a scratch directory to keep state files out of the repo. The unit tests mock every HTTP call, including The Odds API.
