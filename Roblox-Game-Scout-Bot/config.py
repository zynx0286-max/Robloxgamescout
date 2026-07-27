import os
from dotenv import load_dotenv

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
ALERT_MIN_PLAYERS = int(os.getenv("ALERT_MIN_PLAYERS", "100"))
ALERT_MIN_GROWTH = float(os.getenv("ALERT_MIN_GROWTH", "20"))
ALERT_MAX_VISITS = int(os.getenv("ALERT_MAX_VISITS", "10000000"))
