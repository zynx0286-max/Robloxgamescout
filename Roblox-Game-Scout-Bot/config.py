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
DATABASE_PATH = os.getenv("DATABASE_PATH", "scout_bot.db")

# Scout settings
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "5"))        # Max games returned per search
MIN_ACTIVE_PLAYERS = int(os.getenv("MIN_ACTIVE_PLAYERS", "0"))  # Filter threshold
