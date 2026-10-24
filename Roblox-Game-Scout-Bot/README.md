# Roblox Game Scout

Roblox Game Scout is an acquisition-focused Discord bot that discovers Roblox games with strong commercial potential by combining live Roblox data, Roblox Charts signals, Discord/social link presence, and lightweight scoring.

## What it does

- Scans Roblox games for acquisition-style opportunities
- Enforces acquisition-friendly filters you control from `/settings` — CCU floor and ceiling, visit range, rating floor, growth, Discord invite, optional Roblox Chart presence, genre, and maximum game age
- Runs slash commands in Discord such as /scan, /settings, /profile, and /analyze
- Supports automated scanning and alerting workflows
- Serves live portfolio stats to Framer via a FastAPI endpoint (portfolio_worker.py + portfolio_api.py)

## Features

- Discord slash commands
- Filtered game discovery pipeline
- Async scanning for faster operation
- Diagnostics summary for scan results
- Optional AI-assisted analysis integration

## Project structure

- Roblox-Game-Scout-Bot/ — Discord bot implementation
- artifacts/ — generated artifacts and samples
- lib/ — shared libraries and supporting packages

## Getting started

1. Install Python dependencies
   ```bash
   cd Roblox-Game-Scout-Bot
   python -m pip install -r requirements.txt
   ```
2. Configure environment variables for your bot token and database path
3. Start the bot
   ```bash
   python main.py
   ```

## AI provider (free)

The AI analyst works with any OpenAI-compatible endpoint. Default setup uses
**Groq** (https://console.groq.com) — a genuinely free inference API, no credit
card required, with daily request limits far above Gemini's free tier:

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` | Groq key (recommended primary provider) |
| `GEMINI_API_KEY` | Google Gemini key (fallback provider) |
| `AI_PROVIDER` | `auto` (default: Groq if set, else Gemini), `groq`, or `gemini` |
| `AI_BASE_URL` | OpenAI-compatible base URL (default `https://api.groq.com/openai/v1`) |
| `AI_MODEL` | Model id (default `llama-3.3-70b-versatile`) |
| `GEMINI_DAILY_LIMIT` | Daily AI request cap (default 50) |

With only `GROQ_API_KEY` set, all analysis and AI-scout chat run through Groq.
Any other OpenAI-compatible host (Cerebras, OpenRouter, ...) works by changing
`AI_BASE_URL` and `AI_MODEL` — no code changes.

## Growth history (native)

Roblox publishes no historical CCU/visit series for an experience, so growth
numbers come from **the bot's own snapshots**. Every game in a scan cycle is
written to the `game_history` table, retained for 30 days, and the growth
figures on an embed are computed from the first and last reading inside the
window (`GROWTH_WINDOW_DAYS`, default 7).

There is no third-party analytics dependency. The bot previously pulled a
7-day series from Atlas (atlasdev.gg); that integration, and the dead
RoMonitor Stats / Creator Exchange scrapers, were removed. The own-snapshot
store is deeper (30 days vs 7) and cannot break when an external site changes.

- Discovery alerts get `<N>d CCU` and `<N>d visits` rows in Growth Data.
- Tracker alerts get a `<N>d CCU Trend (tracked)` row and a Roblox link.
- Watched games whose short-term CCU spike contradicts a steep tracked decline
  (> -60%) are logged as `history_decline_warning` so you don't chase a dead game.

One trade-off worth knowing: a game seen for the very first time has no
history yet, so its first alert omits the growth rows instead of printing 0%.
They appear from the second cycle onward. `/watch` a game to start recording
it immediately.

## Discord invite detection

Roblox withdrew the social-link APIs behind authentication — both
`games.roblox.com/v1/games/{id}/social-links/list` and
`groups.roblox.com/v1/groups/{id}/social-links` now return HTTP 401
`{"code":9002,"message":"Authentication token is missing"}`, with or without a
browser User-Agent. The bot therefore finds a Discord invite by matching
`discord.gg/...` in the **game description**, which arrives free with the batch
detail payload — the lookup now costs zero extra requests.

Because of that, `require_discord` defaults to **off**: requiring an invite
would mean requiring something the bot can only see when a developer wrote it
in their description, and almost nothing would ever alert. Turn it on with
`/settings require_discord:Yes` when you specifically want games that advertise
a Discord.

## Auto-watch (alerts keep being tracked)

An alert is a one-time post — the interesting part is what happens next. With
`AUTO_WATCH_ALERTS=true` (the default) the bot adds a **system watch** to
every game it alerts on, so the tracker reports breakouts and declines
afterwards without anyone remembering to press 👀 Watch.

- System watches post **quietly**: they never @-mention anyone, and they are
  labelled `Auto-watched` in the channel. Claim the game (✅) or watch it
  yourself to get pinged.
- `AUTO_WATCH_MAX` (default 60) caps how many system watches exist at once;
  the **oldest** is dropped when the cap is hit. Your own watches are never
  pruned by the cap. Each watched game costs the tracker one Roblox request
  per cycle, which is what the cap bounds.
- `/unwatch <game_id>` removes the bot's automatic watch for a game too — it
  is the escape hatch when you have decided you are not interested.
- `AUTO_WATCH_MAX=0` clears/disables system watches entirely.

## Chart rank as a signal

Roblox Charts is not only a discovery source — **where** a game sits on a
chart, and whether it is climbing, is one of the strongest early acquisition
signals. Every scan cycle, each charted game's position within its sort is
recorded to a local `chart_positions` table (30-day retention). From that:

- Alert embeds show `Chart: #3 on Top Trending · ▲2 vs last cycle · best #1`.
- Scout Score gains a **Chart rank** component (0–10): top-3 = 10 points,
  tapering to 2 points for #50+, plus a +2 bonus for climbing 5+ spots in a
  cycle. This is often a brand-new game's strongest score input, since it has
  neither velocity history nor a favor rate yet.
- Tracker alerts show a `Chart Rank` row read from the same store.
- A game on several charts reports its **best** position.

No extra network requests are involved: positions arrive with the chart
data discovery already fetches, and the deltas are local SQLite reads.

## Discovery note (2026-09)

Roblox **retired** the old `/v1/games/list` and `/v1/games/recommendations`
endpoints (both now return 404), so keyword discovery runs on the official
search API that powers roblox.com/search (`apis.roblox.com/search-api/
omni-search`), and deep scans add trend-flavored keyword queries. Chart-based
discovery uses the same official explore API that powers roblox.com/charts.
Every candidate is stamped with whether it currently appears on a chart, which
backs the optional `require_charts` filter. If discovery volume ever drops,
the curated seed list (`game_search.py`) and the Charts sorts remain as
always-available fallbacks.

## Health checks & tooling

- **Startup self-check**: on boot the bot probes every external dependency
  (Roblox batch API, omni-search, Roblox Charts, AI provider key, Discord
  token) plus its own growth-history store, and logs one line each — a dead
  endpoint is caught in the logs immediately instead of as mysteriously empty
  alerts weeks later. `/status` re-runs the probes on demand in Discord.
- **`/scanping`**: opt in/out of being pinged on scheduled discovery alerts.
  Pings respect your alert level (low-priority alerts only ping "All"
  subscribers) and games you've ❌ rejected never ping you.
- **`python validate_seeds.py`**: verifies every curated seed ID against the
  live Roblox API (`--prune` removes dead ones). The seed list must contain
  *universe* IDs — place IDs silently resolve to unrelated games.
- **CI**: GitHub Actions runs the test suite and a full import smoke test on
  every push (`.github/workflows/ci.yml`).

## Web console (the front door)

The portfolio API also serves a server-rendered Scout console. It reads the
bot's own SQLite stores and never calls Roblox:

| Route | What it is |
| --- | --- |
| `/` | Redirects to `/dashboard` |
| `/dashboard` | Live market board: opportunity scores, fresh CCU movers, chart climbers, Ascent Radar catches, latest alerts and portfolio cards. Auto-refreshes every 5 minutes. |
| `/market` | Market weather: 24h and 7d breadth (advancers, flat, decliners), the median move and a plain-language market state, a fixed-bucket distribution of every 24h move, leaders and laggards, the games furthest below their 30-day peak, per-chart footprint and each portfolio project against the median. Stale reads are left out of breadth rather than counted at an old price. |
| `/games` | Searchable index of every alerted, watched or portfolio game: client-side search, signal filters, sorting, 7d trends and score bars. Press `/` to search; Export CSV downloads exactly the visible rows. |
| `/compare` | Head-to-head view of any two games: pickers (auto-submit), swap, an at-a-glance table that highlights the stronger number, and both games' 24h/7d/30d charts. Deep links work: `/compare?a=101&b=104`. |
| `/watchlist` | The tracking desk: every watched game with its 14-day trend, CCU, 24h/7d and since-watch growth, status (spiking, rising, cooling, quiet, stale), last check and whether the watch is yours or an auto-watch. Search, filter by signals, sort, and `/` to search. |
| `/scorecard` | The bot's own track record: every alert from the last 30 days graded at a fixed 72-hour horizon. Hit rate, outcome spread, a calibration plot of score vs move, hit rate and median move by score band, and every call with its baseline, 72h read and status. Open calls are excluded from the hit rate. |
| `/game/{id}` | One game's local dossier: a **Scout read** that states the 7-day direction, 30-day-peak position, 24h move and every recorded signal in plain language; 24h/7d/30d CCU charts with an optional **Scout-market median** overlay and a **vs Scout market** delta; latest/peak stats; the alert record; its self-graded call outcome; chart positions; watch state; radar history; the cached AI verdict; and a **signal timeline** of its dated events (first snapshot, peak, alert, watch, radar catch, chart position, AI verdict, portfolio addition, latest reading) newest first. |

Design contract: no external assets (inline CSS/SVG/JS only), no network
reads, every interpolated value escaped, and an explicit empty state instead
of mock data. The board renders from the same short-TTL cache as the JSON
feeds; the game index, watchlist and history views use one bulk
`game_history` read rather than one query per game. The dashboard's 5-minute
auto-refresh also preserves scroll position, so a reload does not send you
back to the top. The watchlist never presents an old reading as a live
signal: a tracker check older than six hours is labelled **Stale**, and
since-watch growth only compares snapshots inside the 30-day store. The
market page applies the same six-hour bar to breadth, leaders and
drawdowns, and says how many games it left out.The track record grades against the bot's own snapshots at a fixed 72-hour
horizon, so a call is never re-graded against a later, kinder reading. The
dossier's Scout read is deterministic and local: a reading older than six
hours is labelled **Stale data** and described as historical, never mixed
with a live momentum label. The dashed chart benchmark is the median path
of the games Scout has identified, each indexed to its own first reading
and scaled to the game's start; it is drawn only when at least three games
contribute to a bucket.

```bash
# local
python -m uvicorn portfolio_api:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/dashboard
```

On Render/Docker the console is served by the same service (see below), so
your Framer cards and the dashboard share one data source. The pages are
read-only and include no Discord user identifiers.

## Portfolio live-data (free hosting)

The portfolio stack (`portfolio_worker.py` + `portfolio_api.py`) is designed to run on a free tier. Pick one:

### Recommended: Render (free, GitHub-native)

1. Push this repo to GitHub (already done).
2. In the Render dashboard choose **New > Blueprint**, connect the repo, and confirm the `render.yaml` blueprint.
3. Render builds the Dockerfile and runs one free web service that starts both the snapshot collector and the API via `start.sh`.
4. Your API URL is `<service>.onrender.com` — point Framer at `/api/v1/portfolio`.

Notes on free tier:
- A single service is used on purpose: free instances get their own disk, so a separate worker would write a database the API can't see. `start.sh` keeps both halves in one container.
- Free web services spin down after ~15 min without traffic. Hitting `/api/v1/health` from Framer (or a cron) every few minutes keeps it warm.
- Alternatively run the collector as a cron job (`python portfolio_worker.py --once`) and skip the long-lived worker.

### Docker (any host)

```bash
docker build -t game-scout .
docker run -p 8000:8000 game-scout                    # collector + API
docker run game-scout python portfolio_worker.py --once   # one-off snapshot
```

### Manual (any always-on VM/container)

```bash
# terminal 1 — collector
python portfolio_worker.py --interval 60
# terminal 2 — API
python -m uvicorn portfolio_api:app --host 0.0.0.0 --port 8000
```

## Legal / compliance note

This project is intended for research, analysis, and bot automation workflows. Users are responsible for complying with Roblox, Discord, and third-party platform terms of service, API usage policies, and applicable laws.

## Disclaimer

This repository is provided for informational and development purposes. It does not guarantee the accuracy of market signals or investment or acquisition outcomes.
