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

# Additional scrape sources (comma-separated URLs, can be disabled individually)
ENABLE_ROTRENDS = os.getenv("ENABLE_ROTRENDS", "true").lower() == "true"
ENABLE_ROBLOPOLIS = os.getenv("ENABLE_ROBLOPOLIS", "true").lower() == "true"