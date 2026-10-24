# Hosting Roblox Game Scout 24/7 (for $0)

The bot is a long-running Discord **websocket** process. That rules out most
"serverless"/free-web-app tiers (Render free, Railway trial, Vercel): they
**sleep when idle**, and a sleeping Discord bot is just offline. Below are
the options that genuinely stay on 24/7 for $0, in the order to consider.

On every host, set these env vars (or a `.env` file next to `main.py` —
`config.py` loads it automatically):

| Variable | Required | Notes |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | From the [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot. |
| `GUILD_ID` | recommended | Your test server ID → commands sync instantly and restarts are safe. |
| `ALERT_CHANNEL_ID` | ✅ | Channel that receives automatic alerts. |
| `AI_CHANNEL_ID` | optional | Channel where the AI scout replies to messages. |
| `GROQ_API_KEY` | optional | Free AI analysis — get one at [console.groq.com](https://console.groq.com). Without it the bot still works, just no AI verdicts. |
| `GEMINI_API_KEY` | optional | Second AI provider, free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). **Set both if you can**: free tiers fail constantly and independently, so with two keys a throttled provider hands off to the other instead of downgrading every game to a "Data Only" verdict. Groq answers first, Gemini is the backup. |
| `ROBLOX_API_MIN_INTERVAL` | optional | Seconds between requests to `games.roblox.com` (default `0.4`). **Raise to `1.0` on free/shared hosts** — that host throttles datacenter IPs hard enough to return HTTP 429 within seconds of boot, while `apis.roblox.com` stays healthy. If `/status` shows a 429 here, this is the knob. |
| `DATABASE_PATH` | recommended | Point at a persisted path (see per-host notes). SQLite is included — no DB service needed. |
| `ALERT_FILTER_USER_ID` | optional | Your Discord user ID. With it set, your `/settings` profile also gates the scheduled alerts posted to `ALERT_CHANNEL_ID` — otherwise that channel uses only the `ALERT_*` base thresholds, since it cannot honour every subscriber's personal filters at once. |
| `ALERT_MIN_PLAYERS` / `ALERT_MIN_GROWTH` / `ALERT_MAX_VISITS` / `ALERT_REQUIRE_DISCORD` | optional | Base thresholds for every alert (`15` / `0` / `1500000` / `true`). These were previously declared but never read; they now apply. |
| `GROWTH_WINDOW_DAYS` | optional | Days of own-snapshot history behind the growth figures (default `7`; history is retained for 30). |
| `ASCENT_CHECK_INTERVAL_MINUTES` / `ASCENT_MIN_CCU` / `ASCENT_SPIKE_RATIO` / `ASCENT_MIN_WINDOW_RISE_RATIO` / `ASCENT_COOLDOWN_MINUTES` / `ASCENT_MAX_ALERTS_PER_CYCLE` / `ASCENT_ENABLED` | optional | Ascent Radar knobs (default `5` / `50` / `2.0` / `1.5` / `45` / `5` / `true`). See "Ascent Radar" below — the defaults work out of the box; you only need these to tune noise or silence it entirely. |
| `LEDGER_ENABLED` / `LEDGER_WEEKDAY` / `LEDGER_HOUR_UTC` | optional | Weekly Ledger knobs (default `true` / `1` / `13`). See "Weekly Ledger" below — posts once per ISO week to the alert channel; silence with `LEDGER_ENABLED=false`. |

Everything else (Roblox APIs, Roblox Charts) needs no keys — the bot talks
only to Roblox, Discord and the AI provider, and growth history is computed
from its own snapshots.

### Filtering: base tier vs. your profile

Two tiers apply to every game, and both are enforced by the same code path:

1. **Base** — the `ALERT_*` values above. Applies to every alert.
2. **Your profile** — whatever you set with `/settings`. Each stored value
   overrides the base one.

`/settings` controls all ten options for real: minimum/maximum CCU,
minimum/maximum visits, minimum growth, minimum rating, Discord invite
required, Roblox Chart presence required, genre, and maximum game age. A
value of `0` on a maximum (or on `max_age` / `min_rating`) means "no limit".
Run `/profile` to see exactly what is in force, and `/scan` with no matches
prints a per-filter funnel showing where games dropped out.

Two defaults changed with this version, both because the old value would have
silently suppressed every alert:

- **`require_discord` is off.** Roblox's social-link APIs, the only place a
game's Discord link used to be readable, now return HTTP 401. An invite is
detectable only when it is written in the game description. Turn it on from
`/settings` if you want that as a requirement.
- **Game age is uncapped (`max_age = 0`).** The stored `365` was never
enforced by any code, so it was not a decision anyone had made — enforcing it
now would have filtered out nearly every acquisition target.

Existing profiles are migrated automatically: those two stored values are
reset once, on the first start after upgrading.

### Auto-watch: the tracker follows every alert

By default the bot now **system-watches** every game it alerts on
(`AUTO_WATCH_ALERTS=true`), so the tracker keeps reporting breakouts and
declines after the one-time alert. The rules that matter on a shared free
host:

- System watches post quietly and never @-mention anyone.
- `AUTO_WATCH_MAX` (default `60`) caps them, because each watched game costs
  the tracker **one Roblox request per cycle**. When the cap is hit the
  oldest system watch is dropped; human watches are never pruned. Lower it
  (e.g. `20`) if `/status` shows 429s you cannot otherwise shake.
- `AUTO_WATCH_MAX=0` clears and disables system watches.
- `/unwatch <game_id>` also lifts the bot's automatic watch for that game.

### Ascent Radar: breakouts between scan cycles

Every `ASCENT_CHECK_INTERVAL_MINUTES` (default **5**) a loop reads the
bot's own 24-hour snapshot history and looks for games whose freshest CCU
reading is at least `ASCENT_SPIKE_RATIO` (2.0×) their recent average AND
at least `ASCENT_MIN_WINDOW_RISE_RATIO` (1.5×) the window's low — a
discontinuity, not just the top of a steady climb. When one qualifies,
a 🚀 **New ascent** line posts to the alert channel at that minute, not
at the next 30–minute scan.

Design guarantees:

- **Zero Roblox requests.** The radar is a pure local read (indexed
  `game_history` query). It cannot make `/status` worse.
- **No double-reporting.** A game the discovery scanner already alerted
  on is escalated ("🔥 Ascent continues") instead of re-announced.
- **No spam.** One post per game per `ASCENT_COOLDOWN_MINUTES` (45) — the
  cooldown is stamped even when the channel is unresolved or a send
  fails, so a fixed channel never greets you with a queued burst — and a
  hard cap of `ASCENT_MAX_ALERTS_PER_CYCLE` (5) per tick, highest spike
  first.
- **Freshness gate.** Only a response younger than the radar interval +
  15 minutes counts, so an old, stale snapshot is never reported as a
  live climb.
- To silence the feature entirely: `ASCENT_ENABLED=false`.

### Weekly Ledger: the bot grading its own calls, published weekly

Once per ISO week (default **Monday** at 13:00 UTC), the bot posts a
report card of its own recent performance to the alert channel:

- **Alert calls** — the same honest grading the `/scorecard` command
  shows (last 72 h at a glance), plus the most notable calls by absolute
  CCU movement.
- **AI verdicts** — the week's AI verdicts graded by stance: a bullish
  🟢 Worth Monitoring was correct when the game grew ≥ +20%; a bearish
  🟠 Risky / 🔴 Avoid was correct when it *didn't*; neutral verdicts are
  never graded. Verdicts on games without a measurable outcome are
  excluded entirely, not scored as misses.

Design guarantees: one post per week (keyed by ISO year-week — restarts
cannot double-post, a missed Monday is caught up later the same week);
the week key is burned **only after a successful send**, so a Discord
hiccup or unresolved channel retries next hourly tick instead of
eating the week; pure local math, zero Roblox requests.

`LEDGER_ENABLED=false` silences it; `LEDGER_WEEKDAY` (ISO weekday,
Mon=1) and `LEDGER_HOUR_UTC` move the slot.

### Pulse: what the bot knows, on demand

`/pulse` prints an instant market read assembled entirely from the bot's
own local data — no Roblox requests, so it is safe to use any time, and it
works even when the Roblox APIs are throttling:

- **CCU movers** — games whose freshest snapshot is up vs. the window
  start (only snapshots younger than 30 minutes count as "moving now").
- **Chart climbers** — games whose `chart_positions` improved between
  cycles (cycle-over-cycle comparisons only; two rows from one cycle are
  never treated as a change).
- **Ascent radar catches** — recent radar posts with their kind
  (🚀 new ascent vs 🔥 escalation).

The same three sections ride along in the **morning digest** automatically,
so the market shape is visible without typing anything. Every section
renders a quiet line when it has no data, and a failing selector never
crashes the command or the digest — the rest of the report still posts.
No configuration needed; there are no PULSE_* env vars.

### Dossier: the one-game drill-in

`/dossier <universe_id>` renders a complete single-game briefing in one
message — useful when an alert fires and you want **everything** before
moving: live Roblox numbers, the bot's own snapshot growth and 72 h
momentum, chart rank and movement, whether the bot alerted it / anyone
watches it / the radar flagged it, the stored AI verdict, and creator
insight from the local developer cache.

The **Scout Read** line at the bottom is deterministic — a fixed rubric
over measured signals, banded 🔥 Strong / 📈 Building / 🌱 Early / ⚪ Flat
with its reasons spelled out. It is free to run, never rate-limited, and
never hallucinated. The stored AI take (when one exists via `/analyze`)
appears alongside it, never instead.

The dossier makes exactly one Roblox request; if it fails, the report
clearly says the live numbers may be stale and still includes every
local section. No configuration, no env vars.

### Scorecard: the bot grading its own calls

`/scorecard` shows how the bot's alert calls from the last 72 hours have
actually performed, computed entirely from its own snapshot trail:

- **✅ Win** — the game's CCU grew ≥ 20% since the alert snapshot.
- **🔕 Quiet** — the game didn't move meaningfully either way.
- **📉 Miss** — the game fell ≤ −20% since the alert.
- **❔ Unrateable** — no usable baseline or latest snapshot. These are
  reported openly and **excluded from the hit rate**, so the aggregate
  number never gets flattered by calls it couldn't grade.

The baseline is always the bot's own alert-time measurement — never a
re-derivation — so the grade answers "did the bot know what it was doing
at the moment it called it?" Zero Roblox requests; safe to run anytime.
No configuration needed.

### Empty scans now say why

When a scan cycle matches nothing, the log (and `/scan` diagnostics) print a
dominant-reason line like `top reasons: Rating (257)`. That is almost always
a data bug — enrichment skipped, an API field renamed — not a dry market.
The startup `/status` probes similarly flag endpoints that answer HTTP 200
with no usable data instead of calling them healthy.

### If AI verdicts keep coming back as "Data Only"

Run `/status` to see which provider is answering, or `/tokencheck` for the
exact provider order. With both keys set the bot tries Groq first and falls
back to Gemini on any failure; `AI_PROVIDER=groq` or `=gemini` pins you to
one and disables the handoff.

### If `/status` shows a 429 on the Roblox games API

That is Roblox rate-limiting the host's IP, not a broken bot. Two things
already handle it: requests to that host are paced behind
`ROBLOX_API_MIN_INTERVAL`, and 429s are retried with exponential backoff.
Ratings and thumbnails are fetched one request per 100 games rather than
one per game for the same reason. If it still shows 429, raise
`ROBLOX_API_MIN_INTERVAL` to `1.0` and restart.

### If the console shows `LoginFailure: Improper token has been passed`

Discord was reached, so the variable **was** delivered — it just is not a
valid **bot** token. The bot strips stray whitespace/quotes and a `Bot `
prefix automatically, so the remaining causes are:

- The **Client Secret** or **Application ID** was pasted instead of the Bot
  Token. The Bot Token lives at: Developer Portal → your app → **Bot** →
  **Reset Token**. It is shown once — copy the whole value (three
  dot-separated segments, 70+ characters).
- The token was **reset after you copied it**: pressing Reset Token
  invalidates every older copy immediately.
- Only part of the token was copied.

Update `DISCORD_TOKEN` in the panel's Startup/Variables tab (or the `.env`
next to `main.py`) and start again — no quotes or spaces around the value.

---

## Option 1 — bot-hosting.net (easiest, ad-supported)

A free host built for Discord bots. ~1 GB RAM on the free plan, 24/7, no
credit card. The "cost" is that free instances are shared/ad-supported —
closest thing to what you asked for ("watch ads to keep it free").

1. Create an account at bot-hosting.net and earn/redeem free credits.
2. Create a **Python** server (Pterodactyl panel).
3. Upload the `Roblox-Game-Scout-Bot/` folder contents (zip → upload →
   unzip in the panel's file manager).
4. In the **Startup** tab set no custom command — panel uses
   `python main.py` — and add the env vars above in the **Variables** section.
5. Start. Check the console for `Slash commands synced` and the startup
   health-check lines (`health: ✅ Roblox games API ...`).

## Option 2 — Oracle Cloud "Always Free" (most reliable, hardest setup)

A real VPS (ARM, up to 4 vCPU / 24 GB RAM) that is free *forever*, no ads.
Requires a credit/debit card for identity verification only and some Linux
comfort. Recommended once you outgrow option 1.

```bash
# on the Ubuntu VM, as ubuntu user:
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo> && cd <repo>/Roblox-Game-Scout-Bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your tokens
# run under systemd so it survives reboots and crashes:
sudo cp deploy/scoutbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now scoutbot
journalctl -u scoutbot -f    # watch logs
```

## Option 3 — free bot hosts with daily renew (wispbyte, heavencloud, …)

Similar to option 1 (Pterodactyl panels, small RAM, occasional click-to-renew
every few days). Same upload steps as option 1. Fine as a backup host.

---

## Shipping a new version (and rolling it back)

Releases are tagged; CI packages and attaches the zip automatically:

1. Confirm `BOT_VERSION` in `config.py` matches the release, commit, then
   `git tag v1.5.0 && git push origin v1.5.0`.
2. The **Release** workflow runs the suite and attaches
   `GameScout-v<version>.zip` to the GitHub release. Download it there —
   that zip is the deploy artifact (no `.env`, no database inside).
3. On the host: stop the service, replace the code (keep the existing
   `scoutbot.db`!), start the service. First boot runs the migrations —
   new tables/indexes (WAL journal, `chart_positions`, hot-path indexes)
   are created in place; existing rows are untouched.
4. Verify: `/status` (all probes green), `/about` (shows the new
   `BOT_VERSION`), and watch one scheduled scan in the console.

**Rollback** = the same steps with the previous version's zip. Every
schema change in this project is `CREATE TABLE IF NOT EXISTS` /
`CREATE INDEX IF NOT EXISTS` / additive `ALTER TABLE`, so an older build
runs against a database written by a newer build without a downgrade
script. Keep the last known-good zip next to the release you are shipping.

## What NOT to use

- **Render free web service** — sleeps after 15 min idle; the bot dies every
  time. The repo's `render.yaml` is for the **portfolio API only** (where
  sleeping + a pinger is fine).
- **Railway / Fly.io free(ish) tiers** — trials/credit-based, not $0 forever.
- **Replit free** — no always-on execution anymore.
- **Your own PC** — works (double-click `run_bot.bat`), but "24/7" ends when
  the PC sleeps; fine for testing only.

## If your host allows only ONE long-running process

The bot, the portfolio worker, and the portfolio API can all live in the
same container/process tree. Run the bot as the main process:

```bash
python portfolio_worker.py --interval 60 &   # portfolio snapshots
python -m uvicorn portfolio_api:app --host 0.0.0.0 --port 8000 &  # portfolio API
python main.py                                # the bot (foreground)
```

They share the SQLite file, so nothing else is needed. (Only worth it if you
also want the Framer portfolio API on the same host; otherwise just run the
bot.)

## Keeping the database safe

SQLite lives in the container/VM disk. On Pterodactyl-style hosts the disk
persists across restarts; on anything ephemeral, set `DATABASE_PATH` to a
mounted/persisted directory and back up `scoutbot.db` occasionally
(`manage_portfolio.py` data lives there too).
