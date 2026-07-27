"""#ai-scout channel listener — runs the same analyze_game() flow used by
/analyze, but driven by anyone posting a universe id in the configured
private channel. Reuses all existing modules so cache, quota gate, scout
score, developer intelligence, and Gemini response format are 100%
consistent with the slash command.
"""

import asyncio
import logging
import os
import re

from roblox_api import get_game_info
from growth import get_growth
from scanner import calculate_scout_score
from trend import calculate_trend_score
from gemini_analyzer import analyze_game, format_report

logger = logging.getLogger("ai_scout_channel")

AI_SCOUT_ID_ENV = "AI_SCOUT_ID"
_DIGIT_RE = re.compile(r"\b\d{5,}\b")


def get_ai_scout_channel_id():
    """Read AI_SCOUT_ID from env. Returns 0 when unset or invalid."""
    raw = os.environ.get(AI_SCOUT_ID_ENV)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def handle_ai_scout_message(message):
    """Process a single Discord message.

    Returns the response text the bot should reply with, or None for silence.
    The channel filter means this function early-exits on every regular
    channel, so it is safe to bind to on_message globally.
    """
    if message.author.bot:
        return None

    if message.channel.id != get_ai_scout_channel_id():
        return None

    candidates = _DIGIT_RE.findall(message.content or "")
    if not candidates:
        return (
            "👋 Mention me with a Roblox Universe ID to get an AI analysis.\n"
            "Example: `@Roblox Game Scout 994732206`"
        )

    try:
        universe_id = int(candidates[0])
    except ValueError:
        return None

    try:
        info = await asyncio.to_thread(get_game_info, str(universe_id))
    except Exception as exc:
        logger.exception("Failed fetching game info for %s", universe_id)
        return f"⚠️ Failed to fetch game info: `{exc}`"

    if info is None:
        return (
            f"❌ Could not find a Roblox game with Universe ID `{universe_id}`."
        )

    growth = get_growth(info["id"])
    info["growth"] = growth
    info["scout_score"] = calculate_scout_score(info)
    info["trend_score"] = calculate_trend_score(info, growth)

    # analyze_game handles cache, quota gate, and fallback internally.
    analysis = analyze_game(info, force_refresh=False)
    return format_report(info["name"], analysis)
