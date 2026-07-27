"""#ai-scout personal assistant channel listener.

Two response modes:

1. ``@Roblox Game Scout <universe_id>`` (or ``... analyze <id>``) — runs the
   ``analyze_game()`` pipeline and posts the same ``format_report()`` output
   as the ``/analyze`` slash command. Cache + quota gate + Scout Score +
   Gemini verdict all unchanged.

2. ``@Roblox Game Scout <question>`` — freeform Gemini question. Useful for
   asking about recent scans, watcher alerts, or general Roblox scouting.

Behavior:
  * Only responds inside the configured channel (``AI_CHANNEL_ID`` per the
    spec, with ``AI_SCOUT_ID`` accepted as a legacy alias).
  * Only responds when the bot is mentioned.
  * Bot-authored messages are always skipped.
  * Slash-command work, scheduler, tracker, filters, and buttons are not
    touched.
"""

import asyncio
import logging
import os
import re

from roblox_api import get_game_info
from growth import get_growth
from scanner import calculate_scout_score
from trend import calculate_trend_score
from gemini_analyzer import analyze_game, answer_question, format_report

logger = logging.getLogger("ai_scout_channel")

AI_CHANNEL_ID_ENV = "AI_CHANNEL_ID"
AI_SCOUT_ID_ENV = "AI_SCOUT_ID"  # legacy alias from initial secret add.
_DIGIT_RE = re.compile(r"\b\d{5,}\b")
_MENTION_RE = re.compile(r"<@!?\d+>")


def resolve_ai_channel_id():
    """Prefer ``AI_CHANNEL_ID`` per spec; fall back to ``AI_SCOUT_ID``."""
    for key in (AI_CHANNEL_ID_ENV, AI_SCOUT_ID_ENV):
        raw = os.environ.get(key)
        try:
            value = int(raw)
            if value:
                return value
        except (TypeError, ValueError):
            continue
    return 0


def _strip_mentions(text):
    """Strip Discord user mentions so the bot only sees the user's question."""
    return _MENTION_RE.sub("", text or "").strip()


def _is_bot_mentioned(message, bot_user_id):
    """True if a Discord user mention token references this bot."""
    if not bot_user_id:
        return False
    for m in _MENTION_RE.finditer(message.content or ""):
        digits = "".join(ch for ch in m.group(0) if ch.isdigit())
        if digits and int(digits) == bot_user_id:
            return True
    return False


async def _handle_analyze_path(universe_id):
    """Fetch + score + run analyze_game and return the formatted report."""
    try:
        info = await asyncio.to_thread(get_game_info, str(universe_id))
    except Exception as exc:
        logger.exception("Failed fetching game info for %s", universe_id)
        return f"⚠️ Failed to fetch game info: `{exc}`"

    if info is None:
        return (
            f"❌ Could not find a Roblox game with Universe ID "
            f"`{universe_id}`."
        )

    growth = get_growth(info["id"])
    info["growth"] = growth
    info["scout_score"] = calculate_scout_score(info)
    info["trend_score"] = calculate_trend_score(info, growth)

    # analyze_game handles cache + quota gate + fallback internally.
    analysis = analyze_game(info, force_refresh=False)
    return format_report(info["name"], analysis)


async def _handle_freeform_path(question):
    """Send a freeform question to Gemini. Returns markdown text or hint."""
    if not question.strip():
        return (
            "👋 Mention me with a game ID to analyze, or ask a Roblox "
            "scouting question.\n"
            "Examples:\n"
            "• `@Roblox Game Scout 994732206`\n"
            "• `@Roblox Game Scout what type of games are trending right now?`"
        )
    return await asyncio.to_thread(answer_question, question)


async def handle_ai_scout_message(message, bot_user_id):
    """Process a single Discord message in the configured #ai-scout channel.

    Returns the response text the bot should reply with, or None for
    silence. The channel + mention filters early-exit on regular channels
    so this is safe to bind to ``on_message`` globally.
    """
    if message.author.bot:
        return None

    if message.channel.id != resolve_ai_channel_id():
        return None

    if not _is_bot_mentioned(message, bot_user_id):
        return None

    cleaned = _strip_mentions(message.content)
    candidates = _DIGIT_RE.findall(cleaned)
    if candidates:
        try:
            universe_id = int(candidates[0])
            return await _handle_analyze_path(universe_id)
        except ValueError:
            pass

    return await _handle_freeform_path(cleaned)
