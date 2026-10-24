import os

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency in some environments
    def load_dotenv():
        return False


load_dotenv()

# Bot settings
# (The bot is slash-command-only; there is no prefix configuration.)
TOKEN = os.getenv("DISCORD_TOKEN")

# Bumped whenever a deploy changes behaviour, so the startup banner and
# /about always tell you which build is actually running.
BOT_VERSION = "1.17.0"

# Roblox API
ROBLOX_API_BASE = "https://games.roblox.com/v1"
ROBLOX_THUMBNAILS_API = "https://thumbnails.roblox.com/v1"

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "scoutbot.db")

# Scheduler
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))   # automated scan interval (minutes)

# Active window for scheduled scans, in Eastern Time (EST/EDT). Scans only
# run inside this window every day; outside it the loop idles. Set
# SCAN_ACTIVE_HOURS=false (or an end hour <= start hour) to scan 24/7.
SCAN_ACTIVE_HOURS = os.getenv("SCAN_ACTIVE_HOURS", "true").lower() == "true"
SCAN_START_HOUR_ET = int(os.getenv("SCAN_START_HOUR_ET", "10"))   # 10 AM ET
SCAN_END_HOUR_ET = int(os.getenv("SCAN_END_HOUR_ET", "21"))      # 9 PM ET (exclusive)

# Morning digest: one summary per day when the scan window opens.
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "true").lower() == "true"
DIGEST_HOUR_ET = int(os.getenv("DIGEST_HOUR_ET", "10"))          # 10 AM ET
DIGEST_LOOKBACK_HOURS = int(os.getenv("DIGEST_LOOKBACK_HOURS", "24"))
DIGEST_TOP_LIMIT = int(os.getenv("DIGEST_TOP_LIMIT", "10"))      # max games listed

# Discord channel that receives automatic opportunity alerts
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

# Base thresholds applied to scheduled (shared-channel) alerts before any
# per-user settings. These used to be dead config — the scheduler only ran
# the hardcoded filters and read none of them.
ALERT_MIN_PLAYERS = int(os.getenv("ALERT_MIN_PLAYERS", "15"))
ALERT_MIN_GROWTH = float(os.getenv("ALERT_MIN_GROWTH", "0"))
ALERT_MAX_VISITS = int(os.getenv("ALERT_MAX_VISITS", "1500000"))

# Require a valid Discord invite at the base tier. Defaults to false: the
# Roblox social-link APIs that used to expose a game's Discord were withdrawn
# behind authentication (both now return HTTP 401), so an invite can only be
# spotted in the game description. Defaulting to true would demand something
# the bot usually cannot detect, and nothing would ever alert. Set it to true
# (here or per-user with /settings) when you specifically want games that
# advertise a Discord in their description.
ALERT_REQUIRE_DISCORD = os.getenv("ALERT_REQUIRE_DISCORD", "false").lower() == "true"

# Optional: a Discord user ID whose /settings also gate the shared alert
# channel. One channel cannot honour every subscriber's personal filters at
# once, so per-user settings apply to /scan results and to mentions by
# default; set this to have your own profile drive the channel too.
ALERT_FILTER_USER_ID = int(os.getenv("ALERT_FILTER_USER_ID", "0"))

# ---- Auto-watch ----
# An alert is a one-time post; without a follow-up, a game that looked
# interesting at 2 PM is never looked at again. Auto-watch makes the bot
# track every game it alerts on, so the tracker reports breakouts and
# declines afterwards.
#
# The watch is a *system* watch: it posts quietly to the alert channel and
# never @-mentions anyone (the mention rules stay yours — claim the game or
# watch it yourself to get pinged). /unwatch <game_id> removes a bot's
# automatic watch as well as your own.
AUTO_WATCH_ALERTS = os.getenv("AUTO_WATCH_ALERTS", "true").lower() == "true"

# Discord user ID the automatic watches belong to. 0 (the default) means the
# bot itself — a user ID that can never match a real member, which keeps the
# system watches out of every per-user list. Point it at your own ID only if
# you want auto-watches to appear in your /watched list.
AUTO_WATCH_USER_ID = int(os.getenv("AUTO_WATCH_USER_ID", "0"))

# Cap on concurrent system watches. Each watched game costs the tracker one
# Roblox request per cycle, so the cap exists to bound that cost. When the
# cap is hit, the OLDEST system watch is dropped — never a user's own watch.
# 0 clears/disables system watches entirely.
AUTO_WATCH_MAX = int(os.getenv("AUTO_WATCH_MAX", "60"))

# ---- Discovery Engine Settings ----

# Number of keywords to rotate through per scan cycle
SEARCH_KEYWORDS_PER_CYCLE = int(os.getenv("SEARCH_KEYWORDS_PER_CYCLE", "5"))

# Maximum games fetched from Roblox search per keyword (API max is 30)
SEARCH_LIMIT_PER_KEYWORD = int(os.getenv("SEARCH_LIMIT_PER_KEYWORD", "30"))

# Total search results limit across all keywords
SEARCH_TOTAL_LIMIT = int(os.getenv("SEARCH_TOTAL_LIMIT", "150"))

# Max concurrent API requests when fetching game details
MAX_CONCURRENT_FETCHES = int(os.getenv("MAX_CONCURRENT_FETCHES", "20"))

# Minimum seconds between requests to games.roblox.com. That host throttles
# cloud/datacenter IP ranges far harder than residential ones — a shared
# free host can be sitting on HTTP 429 before the first scan finishes.
# Every request to that host is paced by this gap (plus jitter), and 429s
# are retried with exponential backoff. Raise it to ~1.0 if /status still
# shows "Roblox games API — HTTP 429".
ROBLOX_API_MIN_INTERVAL = float(os.getenv("ROBLOX_API_MIN_INTERVAL", "0.4"))

# Retries for a single Roblox request after a 429 or connection error.
ROBLOX_API_MAX_RETRIES = int(os.getenv("ROBLOX_API_MAX_RETRIES", "3"))

# Deep scan interval (multiplier of SCAN_INTERVAL)
DEEP_SCAN_INTERVAL = int(os.getenv("DEEP_SCAN_INTERVAL", "120"))  # minutes

# Growth history: how many days of the bot's own game_history snapshots the
# growth figures on embeds are computed from. A snapshot is recorded for every
# scanned game each cycle, and history is retained for 30 days, so anything up
# to 30 is available. Brand-new games need two snapshots before they report.
GROWTH_WINDOW_DAYS = int(os.getenv("GROWTH_WINDOW_DAYS", "7"))

# Trending detection: minimum historical snapshots before computing velocity
MIN_VELOCITY_SNAPSHOTS = int(os.getenv("MIN_VELOCITY_SNAPSHOTS", "3"))

# Trending detection: CCU spike multiplier above rolling average
VELOCITY_SPIKE_THRESHOLD = float(os.getenv("VELOCITY_SPIKE_THRESHOLD", "1.5"))

# ---- Ascent Radar (early breakout detection between scan cycles) ----
# Every ASCENT_CHECK_INTERVAL_MINUTES a local pass over the bot's own
# game_history snapshots looks for games whose FRESH CURRENT CCU reading is
# out of line with their recent average — a game moving between scan passes.
# Local reads only: the radar never costs a Roblox request.
#
# A candidate needs BOTH of these (vs the newest reading):
ASCENT_CHECK_INTERVAL_MINUTES = int(os.getenv("ASCENT_CHECK_INTERVAL_MINUTES", "5"))
ASCENT_MIN_CCU = int(os.getenv("ASCENT_MIN_CCU", "50"))
ASCENT_SPIKE_RATIO = float(os.getenv("ASCENT_SPIKE_RATIO", "2.0"))
ASCENT_MIN_WINDOW_RISE_RATIO = float(os.getenv("ASCENT_MIN_WINDOW_RISE_RATIO", "1.5"))
#
# Anti-spam:
#  * ASCENT_COOLDOWN_MINUTES: one radar post per game at minimum — even a
#    rocketing game never spams the channel more often than the cooldown.
#  * ASCENT_MAX_ALERTS_PER_CYCLE: hard per-tick cap so a wild market stays
#    readable. Highest spike ratios post first (candidates are pre-sorted).
ASCENT_COOLDOWN_MINUTES = int(os.getenv("ASCENT_COOLDOWN_MINUTES", "45"))
ASCENT_MAX_ALERTS_PER_CYCLE = int(os.getenv("ASCENT_MAX_ALERTS_PER_CYCLE", "5"))
#
# Set FALSE to silence the radar entirely (the loop still starts and exits
# immediately, so nothing else changes).
ASCENT_ENABLED = os.getenv("ASCENT_ENABLED", "true").lower() == "true"

# ---- Weekly Ledger (the bot's self-published report card) ----
# Every LEDGER_WEEKDAY at LEDGER_HOUR_UTC, the bot posts a summary of its
# own recent performance to the alert channel: how its alert calls have
# performed (from the scorecard) and how its AI verdicts have graded
# (bullish verdicts were right if the game grew; bearish verdicts were
# right if it did not). Pure local math — zero Roblox requests.
LEDGER_ENABLED = os.getenv("LEDGER_ENABLED", "true").lower() == "true"
# ISO weekday (Mon=1 ... Sun=7):
LEDGER_WEEKDAY = int(os.getenv("LEDGER_WEEKDAY", "1"))
# Earliest UTC hour the report may post:
LEDGER_HOUR_UTC = int(os.getenv("LEDGER_HOUR_UTC", "13"))

# Roblox Charts uses the official explore API — the same data that powers
# roblox.com/charts. It is the only trending source the bot needs: discovery
# and enrichment are Roblox-native, and growth history is computed from the
# bot's own snapshots (see history.py). The former third-party integrations
# (Atlas, RoMonitor Stats, Creator Exchange, BloxAtlas) were removed: none
# expose a stable public game-list or metric API, so they were either dead
# code or an avoidable external dependency.
ENABLE_ROBLOX_CHARTS = os.getenv("ENABLE_ROBLOX_CHARTS", "true").lower() == "true"

# ---- AI Provider ----
# "auto" prefers Groq (GROQ_API_KEY) and falls back to Gemini (GEMINI_API_KEY).
# "groq" or "gemini" forces one. GROQ is OpenAI-compatible: AI_BASE_URL /
# AI_MODEL let you point at Groq, Cerebras, OpenRouter, or any other
# OpenAI-style endpoint without code changes.
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")

# ---- Portfolio Live-Data System ----
# Worker interval between snapshot collections (seconds). 60 = current
# players refresh roughly every minute; heavier stats ride along.
PORTFOLIO_WORKER_INTERVAL = int(os.getenv("PORTFOLIO_WORKER_INTERVAL", "60"))

# How long the portfolio API keeps a cached response (seconds). 30-120 is
# the sweet spot: fresh enough for a "Updated N seconds ago" badge without
# hammering Roblox on every visitor.
PORTFOLIO_CACHE_SECONDS = int(os.getenv("PORTFOLIO_CACHE_SECONDS", "60"))

# Comma-separated CORS origins allowed to call the public portfolio API
# directly from the browser (Framer custom code). "*" allows any origin;
# data served is intentionally public.
PORTFOLIO_CORS_ORIGINS = os.getenv("PORTFOLIO_CORS_ORIGINS", "*")

# Bind host/port for `uvicorn portfolio_api:app` / the worker's API runner.
# `PORT` is set by most hosting providers (Render, Fly, Railway) and wins.
PORTFOLIO_API_HOST = os.getenv("PORTFOLIO_API_HOST", "0.0.0.0")
PORTFOLIO_API_PORT = int(os.getenv("PORT", os.getenv("PORTFOLIO_API_PORT", "8000")))