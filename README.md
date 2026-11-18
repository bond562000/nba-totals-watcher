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

## State

Slate state lives on a branch named `state`, as `state-YYYY-MM-DD.json` (the NBA date, which stays on the previous calendar date until 6:00 AM ET). The file holds:

- `slate_posted`
- per-game `posted`, `evals`, and the last eval time
- `odds_calls` and `credits_remaining`
- `pregame` totals and `pregame_done`

The job restores that file at start, the watcher rewrites it as games are evaluated, and a side loop pushes it to `state` about every 30 seconds and again when the job exits. The next run of the same night sees the same flags, so it does not post the slate twice, does not post a second card for a game, and does not buy a second pregame line.

`--dry-run` writes `state-YYYY-MM-DD-dryrun.json`. When the live file already has the pregame snapshot, the dry run reuses it. When the dry run is the one that takes the snapshot, that snapshot and the call count are copied onto the live file so the night still spends one pregame credit. Dry-run posts do not set the live `slate_posted` or per-game `posted` flags.

This workflow has no `push` trigger, so commits on `state` do not start it. Pushes made with the workflow's `GITHUB_TOKEN` do not start workflows anyway.

## Rules

- Trigger window: 3rd quarter with 8:00 or less on the clock, or 4th quarter with 9:00 or more.
- Projection blends live pace (60%) with a 4.75 points-per-minute baseline (40%). Margin of 20 or more trims the rest-of-game rate by 7%. Margin of 6 or less adds 3 points.
- A play needs an edge of at least 3.5. Frank passes an OVER in a blowout and an UNDER in a close game.
- One Telegram pick per game per night. At most two evaluations per game, at least 6 minutes apart. A quota skip does not use up an evaluation.
- Odds API budget: one pregame call per slate, at most 15 calls a night, and no call when fewer than 25 credits are left. "Reusing saved pregame totals" is logged once per process.

## Local commands

```bash
python3 watcher.py --simulate
python3 -m unittest tests.test_watcher -v
```

`--simulate` fabricates four games and prints the slate card and the picks. It does not touch the network.

```bash
BALLDONTLIE_API_KEY=... python3 watcher.py --check
BALLDONTLIE_API_KEY=... ODDS_API_KEY=... TELEGRAM_CHANNEL_ID=... python3 watcher.py --dry-run
```

`--check` is one scoreboard poll. `--dry-run` follows the live loop and prints cards instead of posting. Point `NBA_STATE_DIR` at a scratch directory to keep state files out of the repo. The unit tests mock every HTTP call, including The Odds API.
