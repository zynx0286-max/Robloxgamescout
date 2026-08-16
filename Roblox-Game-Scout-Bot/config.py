import os

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency in some environments
    def load_dotenv():
        return False


load_dotenv()

# Bot settings
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")

# Roblox API
ROBLOX_API_BASE = "https://games.roblox.com/v1"
ROBLOX_THUMBNAILS_API = "https://thumbnails.roblox.com/v1"

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "scoutbot.db")

# Scout settings
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "5"))        # Max games returned per search
MIN_ACTIVE_PLAYERS = int(os.getenv("MIN_ACTIVE_PLAYERS", "0"))  # Filter threshold

# Scheduler
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "15"))   # automated scan interval (minutes)

# Discord channel that receives automatic opportunity alerts
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

# Default alert filters (used when no per-user settings apply)
ALERT_MIN_PLAYERS = int(os.getenv("ALERT_MIN_PLAYERS", "15"))
ALERT_MIN_GROWTH = float(os.getenv("ALERT_MIN_GROWTH", "0"))
ALERT_MAX_VISITS = int(os.getenv("ALERT_MAX_VISITS", "1500000"))

# ---- Discovery Engine Settings ----

# Number of keywords to rotate through per scan cycle
SEARCH_KEYWORDS_PER_CYCLE = int(os.getenv("SEARCH_KEYWORDS_PER_CYCLE", "5"))

# Maximum games fetched from Roblox search per keyword (API max is 30)
SEARCH_LIMIT_PER_KEYWORD = int(os.getenv("SEARCH_LIMIT_PER_KEYWORD", "30"))

# Total search results limit across all keywords
SEARCH_TOTAL_LIMIT = int(os.getenv("SEARCH_TOTAL_LIMIT", "150"))

# Max concurrent API requests when fetching game details
MAX_CONCURRENT_FETCHES = int(os.getenv("MAX_CONCURRENT_FETCHES", "20"))

# Deep scan interval (multiplier of SCAN_INTERVAL)
DEEP_SCAN_INTERVAL = int(os.getenv("DEEP_SCAN_INTERVAL", "120"))  # minutes

# Trending detection: minimum historical snapshots before computing velocity
MIN_VELOCITY_SNAPSHOTS = int(os.getenv("MIN_VELOCITY_SNAPSHOTS", "3"))

# Trending detection: CCU spike multiplier above rolling average
VELOCITY_SPIKE_THRESHOLD = float(os.getenv("VELOCITY_SPIKE_THRESHOLD", "1.5"))

# Trending/discovery sources. Roblox Charts uses the official Roblox
# explore API (same data that powers roblox.com/charts). RoMonitor Stats
# and Creator Exchange have no public game-list APIs, so they are used for
# per-game analytics links on embeds rather than as discovery scrapers.
ENABLE_ROBLOX_CHARTS = os.getenv("ENABLE_ROBLOX_CHARTS", "true").lower() == "true"
ENABLE_ROMONITOR = os.getenv("ENABLE_ROMONITOR", "true").lower() == "true"
ENABLE_CREATOR_EXCHANGE = os.getenv("ENABLE_CREATOR_EXCHANGE", "true").lower() == "true"

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
PORTFOLIO_API_HOST = os.getenv("PORTFOLIO_API_HOST", "0.0.0.0")
PORTFOLIO_API_PORT = int(os.getenv("PORTFOLIO_API_PORT", "8000"))