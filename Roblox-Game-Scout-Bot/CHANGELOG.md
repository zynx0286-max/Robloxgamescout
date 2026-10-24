# Changelog

Notable changes to the Roblox Game Scout bot. Versions follow the
`BOT_VERSION` constant in `config.py` (shown in the startup banner and
`/about`).

## 1.17.0 — The tape on every chart (2026-10)

Absolute growth finally has a benchmark. Every dossier and compare chart
can now show how the game moved against the Scout market.

- **Scout-market median** — a dashed blue line on the 24h, 7d and 30d
  charts. Each tracked game is indexed to its own first reading in the
  window, the median is taken per time bucket, and the result is scaled
  to this game's starting value so both lines share one axis.
- **Bucket coverage is explicit** — 30-minute buckets over 24 hours,
  two-hour buckets over 7 days and six-hour buckets over 30 days. A
  bucket needs at least three contributing games; without that coverage
  the line is not drawn and the page says nothing. The legend names the
  number of games behind the line.
- **`vs Scout market`** — the chart header reports the difference
  between the game's window move and the median market move in
  percentage points, so +12% reads differently when the tape is +14%.
- **Cached once, shared everywhere** — the benchmark universe is alerts +
  watches + portfolio projects, cached for five minutes and keyed by the
  newest history row, so the game page and both compare charts share one
  bulk read rather than three.
- **A visible method note** under the legend explains the indexing and
  scaling; no hidden normalisation, no invented comparison.
- 4 new page tests cover normalisation and the three-game floor, the
  rendered overlay and vs-market delta, the thin-market empty case and
  the compare charts. Suite now 575 tests.

## 1.16.0 — The dossier reads back (2026-10)

A game page no longer makes you assemble the story from six panels.

- **Scout read** — a deterministic, local-only paragraph at the top of
  every dossier. It states the 7-day direction, where the latest CCU sits
  against the game's own 30-day peak, the 24-hour move, the latest
  reading, the alert that fired, its best chart position, any radar catch
  and its watch count. No LLM, no network, and every clause is a number
  already in the page.
- **Honest badges** — Rising, Cooling, Steady, Early read or Stale data.
  The stale label outranks momentum: a reading older than six hours is
  called historical, not a live signal.
- **Signal timeline** — the dated events Scout holds for the game, newest
  first: first snapshot, 30-day peak, alert, tracking start, radar catch
  and escalation, chart positions, cached AI verdict, portfolio addition
  and the latest reading. Empty histories render an explicit empty state.
- **No extra reads** — the read and timeline are built from the same
  detail payload the page already loads; the watch aggregate now also
  carries the first watch date, with no schema change.
- 4 new page tests cover the momentum read, the stale override, timeline
  ordering and the no-history empty state. Suite now 571 tests.

## 1.15.0 — Market weather (2026-10)

The console can now read the whole tape, not just one game at a time.

- **`/market`** — market breadth from the bot's own snapshots: how many
  fresh games are up, flat or down over 24 hours and 7 days, the median
  move, and a plain-language state (Risk-on, Leaning up, Two-way tape,
  Cooling, Risk-off) that never calls a market from fewer than three
  fresh pairs.
- **Move distribution** — every fresh 24-hour move in fixed buckets, so
  the shape of the day is comparable from one visit to the next instead
  of a histogram whose scale silently changes.
- **Leaders and laggards** — the biggest gainers and decliners with a
  fresh read. A leaderboard only lists moves of the right sign: a
  falling game is never presented under "biggest gains".
- **Where the air is thin** — the games furthest below their own 30-day
  peak, with their latest CCU, peak and 24-hour move, so a fading
  breakout is visible before it becomes a dead portfolio project.
- **Chart footprint** — how many of the bot's games sit on each Roblox
  chart and the best position recorded in the last 48 hours.
- **Your book vs the tape** — every portfolio project against the median
  24-hour move, in percentage points, so "down 5%" has context.
- **Honest freshness** — a reading older than six hours is excluded from
  breadth, leaders and drawdowns instead of being counted at an old
  price; the page says how many games it left out.
- Market joins the primary nav and the dashboard's Market pulse links to
  the full read. Below 520px the five destinations move to their own
  header row rather than overflowing a 320px screen.
- 6 new tests cover the empty state, breadth/median math, the
  never-a-gainer-as-laggard rule, escaping, fixed verdict thresholds and
  half-open histogram buckets. Suite now 567 tests.

## 1.14.0 — The track record (2026-10)

The console can now answer the question every signal product avoids: are
the calls any good?

- **`/scorecard`** — every alert from the last 30 days graded at a fixed
  72-hour horizon against the bot's own snapshots. Calls younger than 72h
  stay Open and are excluded from the hit rate; matured calls without a
  comparable reading are Unrateable, not quietly counted as losses.
- **Outcome spread** — wins, quiet calls and misses in one bar, with the
  counts and percentages behind it. A +20% move is a win, a −20% move is
  a miss, and everything between is quiet.
- **Calibration plot** — score against 72-hour move, one dot per graded
  call, so a real score edge (or the absence of one) is visible at once.
- **Score bands** — hit rate and median move for 90+, 80–89, 70–79 and
  below 70, plus a small-sample-aware read on whether the top band is
  actually outperforming the rest.
- **Every call** — the alert date, score, baseline CCU, 72h CCU, move,
  call status and latest reading, newest first. The dashboard, topbar and
  footer all link to it.
- 4 new page/data tests cover empty, graded, escaping, open-call and
  threshold behavior. Suite now 561 tests.

## 1.13.0 — The tracking desk (2026-10)

Watching a game now has a place to look every day.

- **`/watchlist`** — every game the bot is tracking (human watches and auto
  watches), with a 14-day sparkline, current CCU, 24h, 7d and since-watch
  growth, the badge that matters (Spiking / Rising / Quiet / Cooling /
  Stale), the last tracker check, and whether the watch is yours or the
  bot's automatic one. Empty and missing-history states are explicit.
- **Honest freshness** — a tracker read older than six hours is labelled
  Stale instead of quietly presented as current; the page header also counts
  stale reads so a dead tracker is visible at a glance.
- **Search, filters and sorting** — find by name or id, filter to your
  watches, auto watches, moving, cooling or stale, and sort by momentum, 7d
  growth, CCU, recency or name. `/` focuses search and Escape clears it.
- Watchlist joins the primary nav on every page; the mobile topbar fits all
  four destinations without overflowing.
- 3 new page tests cover the empty watchlist, momentum/staleness rendering
  and name escaping. Suite now 557 tests.

## 1.12.0 — Compare, export, and a console that remembers (2026-10)

The console becomes a daily driver instead of a read-only report.

- **`/compare`** — put any two games head to head: a picker with auto-submit,
  a swap button, an at-a-glance table that marks the stronger number in mint
  (CCU, 7d growth, 30d peak, visits, Scout score, alerts, chart rank, call
  grade, AI verdict), and both games' 24h/7d/30d charts side by side. Deep
  links work (`/compare?a=101&b=104`), the default is the top two by score,
  and the whole page is local data.
- **CSV export** on `/games` exports exactly the rows the current search and
  signal filter are showing — game id, name, score, CCU, 7d growth, alerts,
  last seen and signal flags — with a UTF-8 BOM for spreadsheet apps.
- **Keyboard habits** — `/` jumps to the game search, Escape clears it, and
  the export button confirms with "Exported ✓" without leaving the page.
- **Refresh without losing your place** — the dashboard's 5-minute
  auto-refresh now saves and restores scroll position per path, so a reader
  halfway down the market board stays there.
- Compare links are in the primary nav on every page; the mobile topbar
  trades the decorative LIVE chip for the third destination.
- 6 new page tests cover compare rendering/defaults/same-id/empty/escaping,
  plus the export and compare affordances.

## 1.11.0 — The Scout console (2026-10)

The bot's data now has a front door. The old three flat HTML pages were
replaced with a server-rendered web console (`web_ui.py`) served by the
portfolio API, still with zero external assets and zero Roblox requests.

- **`/dashboard`** — a live market board: the highest-scoring alerted games,
  fresh 24h CCU movers with sparklines, cycle-over-cycle chart climbers,
  Ascent Radar catches, newest alerts and portfolio cards. The hero shows
  the strongest current signal; the header says how fresh the last scan is.
- **`/games`** — a searchable index of every game the bot has alerted,
  watched or put on the portfolio, with signal filters (alerted, watched,
  charts, rising, falling, AI verdict, portfolio), client-side sorting and
  7-day sparklines. Filtering happens in the browser against server-rendered
  rows; the full list still renders without JavaScript. `/` focuses search,
  Escape clears it, and Export CSV downloads exactly the visible rows.
- **`/game/{id}`** — a full local dossier: 24h/7d/30d CCU charts with real
  time axes and readable phone variants, latest/peak stats, the alert
  record, the call's self-graded outcome, chart positions, watch state,
  radar history and the cached AI verdict (strengths, risks, confidence).
- **`/`** now redirects to the board, and `python portfolio_api.py` works
  again (the shared page helper had swallowed the uvicorn entrypoint).
- Dashboard reads stay bulk (one history query serves all sparklines) and
  degrade to explicit empty states if a table is missing; names, AI text,
  chart labels and attributes are escaped.
- 7 new page tests cover the redirect, market sections, index filter
  metadata, chart/AI detail rendering, window selection and escaping.
  Suite now 548 tests.

## 1.10.0 — Ledger (2026-10)

Accountability made continuous: the bot now publishes its own performance
weekly, unprompted — and grades its expensive AI verdicts specifically.

- **The Weekly Ledger**: every `LEDGER_WEEKDAY` (ISO weekday, default
  Monday=1) at `LEDGER_HOUR_UTC` (default 13), a scheduler loop posts a
  report card to the alert channel: the scorecard's alert-call record,
  the most notable calls (top |ΔCCU|), and the week's **AI verdict
  grading** — bullish verdicts (🟢 Worth Monitoring) were right when the
  game grew ≥ +20%; bearish verdicts (🟠 Risky / 🔴 Avoid) were right
  when it *didn't*; neutral verdicts are never graded. Pure local math,
  zero Roblox requests.
- **Honest publish gating.** The post is due at/after the configured
  weekday+hour, keyed by ISO year-week: a mid-week restart cannot
  double-post, a missed Monday is caught up later in the same week, and
  the week key is burned **only after a successful send** — a failed
  attempt (bad channel, Discord hiccup) retries next hourly tick
  instead of silently eating the week.
- **Honest data staging.** AI verdicts are only graded on games whose
  outcome is measurable (a comparable baseline→latest snapshot pair on
  file); unmeasurable calls stay out entirely rather than padding the
  record with noise rows.
- New knobs: `LEDGER_ENABLED` / `LEDGER_WEEKDAY` / `LEDGER_HOUR_UTC`
  (defaults `true` / `1` / `13`).
- 26 new tests (`tests/test_ledger.py`): AI-verdict stance grading,
  honest denominators, report assembly with quiet lines, fail-soft
  staging, the due-moment gate (including non-Monday cadences),
  double-post guard, retry-after-failure, and scheduler registration.
- Suite now 314 tests.

## 1.9.0 — Scorecard (2026-10)

The accountability layer: the bot now grades its own calls.

- **`/scorecard`** (new): for every game alerted in the last 72 hours,
  compares the snapshot the bot measured **at alert time** against the
  freshest snapshot and renders per-call verdicts — ✅ Win (≥ +20% CCU),
  🔕 Quiet, 📉 Miss (≤ −20%), ❔ unrateable — plus an aggregate hit rate
  that excludes unrateable calls from the denominator, so a wave of
  zero-baseline alerts cannot fake a perfect record.
- Baselines are never re-derived: they are the bot's own alert-time
  measurement (snapshot at-or-before the alert timestamp), so the
  grade is honest about what the bot actually knew when it called it.
- Zero Roblox requests — pure local math over `alerted_games` and
  `game_history`, free to run any time.
- 16 new tests (`tests/test_scorecard.py`): the rubric bands, the
  baseline-must-predate-the-alert rule, hit-rate honesty, renderer
  formatting (one symbol per line), fail-soft degradation on empty
  tables, and command registration.
- `/about` now lists the full read-surface (`/pulse`, `/dossier`,
  `/scorecard`). Suite now 288 tests.

## 1.8.0 — Dossier (2026-10)

- **`/dossier <game_id>`** (new): a complete briefing on one game in a
  single message — live Roblox state, the bot's own snapshot history
  (window growth + 72 h momentum), chart presence with movement, ledger
  state (alerted? watched? radar-flagged?), the stored AI verdict, and
  creator insight from the local developer cache.
- **A verdict you can always trust.** The dossier's Scout Read is a
  deterministic narrative composed from a fixed rubric over measured
  signals (momentum spike ratio, chart presence, window growth, ledger
  state → bands 🔥 Strong / 📈 Building / 🌱 Early / ⚪ Flat, each with
  its stated reasons). No LLM call, no cost, no flakiness — the stored
  AI take rides alongside it when one exists, never instead.
- **Fail-soft by construction.** The orchestrator makes exactly ONE
  Roblox request; if it fails, the dossier degrades to a stale-state
  note ("the numbers below may be stale") and still delivers every
  local section. Names resolve from the bot's own ledgers when Roblox
  is unreachable.
- Local-only creator insight: uses the 24 h developer cache — no extra
  network calls, so /dossier can never make the host's 429s worse.
- 14 new tests (`tests/test_dossier.py`): narrative determinism and
  rubric bands, scoreboard rendering and full degradation, ledger
  accuracy (human vs auto watchers), stored-verdict roundtrip, and the
  orchestrator's fail-soft contract including the Roblox-down path.
- Suite now 272 tests.

## 1.7.0 — Pulse (2026-10)

- **`/pulse`** (new): an instant read of the bot's own intelligence — top
  CCU movers over the last 24h (from `game_history`), games climbing the
  Roblox charts cycle-over-cycle (from `chart_positions`), and the Ascent
  Radar's recent catches (from `ascent_state`). Zero Roblox requests:
  the pulse is a pure local read and cannot make `/status` worse.
- **Morning digest gains a Pulse section**: the same movers / climbers /
  radar lines ride along with the daily summary, so the channel sees the
  current market shape every morning without anyone typing a command.
- Names resolve from the bot's own records (`alerted_games`, then
  `watched_games`) — no Roblox lookups anywhere in the pipeline.
- Every section degrades gracefully: empty data renders an explicit quiet
  line, not a blank section or a crash; a failing selector logs a
  warning and the report survives.
- 12 new tests (`tests/test_pulse.py`): pure renderers, selector
  behavior against a seeded database (including the fresh-read gate),
  digest pollution guards, and escape hatches (corrupt-table resilience).
- Suite now 251 tests.

## 1.6.0 — Ascent Radar (2026-10)

The headline: the bot now catches breakouts **between** scan cycles, not
just at them.

- **Ascent Radar** (`ascent.py`, new): every 5 minutes a local pass over
  the bot's own `game_history` snapshots looks for games whose freshest
  CCU reading is out of line with their recent average AND net-risen over
  the window. Costs zero Roblox requests — it reuses data the scanner
  already recorded. Scheduled discovery posts a one-time alert; the
  radar is what says "it is STILL climbing, right now".
- A game the scanner already alerted on has its climb **escalated**
  ("🔥 Ascent continues") instead of re-announced — the ledger's one
  alert per game is respected; the radar reports what it cannot.
- Anti-spam by design: per-game 45-minute cooldown (stamped even when
  the alert channel is unresolved, so no burst when the channel returns),
  per-tick cap of 5 alerts (highest spike first), freshness gate (a stale
  old snapshot is never re-reported as a live climb), and
  `ASCENT_ENABLED=false` to silence the lot.
- New `ascent_state` table (additive, migrates on first boot) + hot-path
  index; eight `ASCENT_*` env vars documented in `DEPLOY.md` and
  `.env.example`. Defaults work out of the box; tune with env only.
- 14 new tests (`tests/test_ascent.py`): detection math, freshness gate,
  cooldown lifetime, upsert behavior, escalation classification,
  rendering, per-cycle cap, channelless-stamping, and an end-to-end
  tick. Suite now 239 tests.

## 1.5.1 — Bad-token survival kit (2026-10)

A wrong token is the most common way a first deploy dies, and it used to
die as a bare `LoginFailure` traceback on the host console.

- `DISCORD_TOKEN` and `GUILD_ID` are cleaned of the whitespace and quote
  characters hosting-panel input fields tend to keep, plus a `Bot ` prefix
  from Discord's own HTTP examples. Cleaning is reported in the log, so a
  value that "looked right" in the panel works instead of failing.
- A token that cannot be a bot token (no three dot-separated segments)
  logs a WARNING naming the usual mix-ups before login is attempted.
- A token Discord rejects now exits with a plain-language explanation —
  Client Secret vs Bot Token, a token reset after copying, a half-copied
  value — instead of a raw discord.py traceback. Exit code stays 1 so the
  host still records the crash.

## 1.5.0 — Hardening release (2026-10)

Interaction reliability

- Alert buttons are now `discord.ui.DynamicItem` subclasses: each button's
  custom id carries the game id (`alert_claim:12345`) and is matched by a
  class regex template in any session. Presses on alerts posted before a
  bot restart used to fail silently (discord.py matches persistent views
  by exact custom id only). **Requires discord.py ≥ 2.4.0.**
- ✅ Claim now bookmarks the game; ✅ Accept bookmarks **and** tracks it —
  previously both buttons did the same thing.
- The AI-channel analysis path (`@bot <universe_id>`) runs the blocking
  Gemini call in a worker thread like every other call site, so a slow AI
  round-trip can no longer stall the bot's event loop.

Alert correctness

- A game is marked alerted only **after** its alert posts successfully.
  Previously a failed `channel.send` — or an unresolved
  `ALERT_CHANNEL_ID` — permanently consumed the game's one-time alert.
- The tracker now also alerts on **declines** (≤ −50% CCU or ≤ −30%
  visits, with dedicated embed styling), not only on growth.
- A non-200 response from a Roblox Charts sort (the "Sort not found or
  not enabled" case) logs a visible WARNING naming the sort, instead of a
  DEBUG line nobody would ever see.

Operational hardening

- `/scan` is guarded by a process-wide single-scan lock plus a 5-minute
  per-user cooldown; `/analyze` by a 1-minute cooldown. Cooldown
  rejections answer with a friendly ephemeral message.
- Both scheduler loops wait for full Discord readiness before acting.
- SQLite runs in WAL mode with a 30 s busy timeout, so the bot and the
  portfolio worker/API can share one database file safely.
- `scan_debug.log` rotates at 5 MB instead of growing forever.
- CI runs the test suite on Python 3.11 (the Docker deploy target) and
  3.12.

Cleanup

- Removed unused dependencies (`aiosqlite`, `beautifulsoup4`) and dead
  code (`game_id_hash`, `BOT_PREFIX`, an orphaned shutdown helper).
- `/about` reports the running version; `BOT_VERSION` lives in `config.py`.
