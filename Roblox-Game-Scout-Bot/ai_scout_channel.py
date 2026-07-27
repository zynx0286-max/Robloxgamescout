"""#ai-scout personal assistant channel listener.

Two response modes:

1. ``@Roblox Game Scout <universe_id>`` (or ``... analyze <id>``) — runs the
   ``analyze_game()`` pipeline and posts the same ``format_report()`` output
   as the ``/analyze`` slash command. Cache + quota gate + Scout Score +
   Gemini verdict are unchanged. **No conversation history** is loaded —
   the analyze path returns a structured report.

2. ``@Roblox Game Scout <question>`` — conversational mode. The bot
   remembers the last few turns in this channel, sends them as context
   alongside the latest question, and reuses the existing
   ``answer_question()`` cache so identical questions get identical cached
   answers (the cache key is the latest message — context isn't part of
   the key, by design, so context doesn't poison the cache).

Behavior:
  * Only responds inside the configured channel (``AI_CHANNEL_ID`` per the
    spec; ``AI_SCOUT_ID`` is accepted as a legacy alias).
  * Responds to anything typed in that channel — no @-mention required.
  * Bot-authored messages are skipped to avoid reply loops.
  * Slash-command work, scheduler, tracker, filters, and buttons are
    untouched.
"""

import asyncio
import logging
import os
import re
import sqlite3

from roblox_api import get_game_info
from growth import get_growth
from scanner import calculate_scout_score
from trend import calculate_trend_score
from gemini_analyzer import analyze_game, answer_question, format_report

logger = logging.getLogger("ai_scout_channel")

from config import DATABASE_PATH

DATABASE = DATABASE_PATH
AI_CHANNEL_ID_ENV = "AI_CHANNEL_ID"
AI_SCOUT_ID_ENV = "AI_SCOUT_ID"  # legacy alias from initial secret add.
HISTORY_CAP = 12  # store last 12 turns (6 user + 6 assistant)
HISTORY_INCLUDE_LAST = 8  # include last 8 turns in prompt to keep it tight

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


def _ensure_history_table():
    """Create the conversation history table + index if missing."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ai_chat_history_recent
    ON ai_chat_history (channel_id, id)
    """)
    conn.commit()
    conn.close()


def _history_append(channel_id, role, content):
    """Append a turn to the channel's history. Truncate content defensively."""
    _ensure_history_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO ai_chat_history (channel_id, role, content)
        VALUES (?, ?, ?)
        """,
        (int(channel_id), role, (content or "")[:4000]),
    )
    conn.commit()
    conn.close()


def _history_recent(channel_id, limit):
    """Return up to `limit` most-recent (role, content) tuples, oldest first."""
    _ensure_history_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT role, content FROM ai_chat_history
    WHERE channel_id = ?
    ORDER BY id DESC LIMIT ?
    """, (int(channel_id), int(limit)))
    rows = cursor.fetchall()
    conn.close()
    return list(reversed(rows))


def _history_prune(channel_id, keep):
    """Trim stored history so only ``keep`` most-recent turns remain."""
    _ensure_history_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    DELETE FROM ai_chat_history
    WHERE channel_id = ? AND id NOT IN (
        SELECT id FROM ai_chat_history
        WHERE channel_id = ?
        ORDER BY id DESC LIMIT ?
    )
    """, (int(channel_id), int(channel_id), int(keep)))
    conn.commit()
    conn.close()


def _strip_mentions(text):
    return _MENTION_RE.sub("", text or "").strip()


def _is_bot_mentioned(message, bot_user_id):
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


async def _handle_freeform_path(question, channel_id):
    """Conversational assistant — record user + assistant turns, send
    recent history to Gemini alongside the latest message.

    Cache key is the latest message text. Conversation context is part of
    the *prompt* but not the cache key, so identical first-time questions
    resolve to identical answers regardless of history depth.
    """
    history = _history_recent(channel_id, HISTORY_INCLUDE_LAST)

    _history_append(channel_id, "user", question)
    _history_prune(channel_id, HISTORY_CAP)

    if not question.strip():
        answer = (
            "👋 Mention me with a game ID to analyze, or ask a Roblox "
            "scouting question.\n"
            "Examples:\n"
            "• `@Roblox Game Scout 994732206`\n"
            "• `@Roblox Game Scout what type of games are trending right now?`"
        )
    else:
        try:
            answer = await asyncio.to_thread(
                answer_question, question, False, history
            )
        except Exception as exc:
            logger.exception("answer_question failed for %s", question)
            answer = f"⚠️ Assistant error: `{exc}`"

    _history_append(channel_id, "assistant", answer)
    _history_prune(channel_id, HISTORY_CAP)
    return answer


async def handle_ai_scout_message(message, bot_user_id):
    """Process a single Discord message in the configured #ai-scout channel.

    Returns the response text the bot should reply with, or None for
    silence. The channel + bot-author filters early-exit on regular
    channels, so binding this to ``on_message`` globally is safe.

    Mentions are no longer required — any user-typed message in the
    configured channel triggers a reply. ``bot_user_id`` is accepted but
    reserved for future mention-pass-through if reintroduced.
    """
    if message.author.bot:
        return None

    if message.channel.id != resolve_ai_channel_id():
        return None

    cleaned = _strip_mentions(message.content)
    if not cleaned:
        # Bare @mention with no body — rephrase as a quick hint.
        cleaned = "?"

    candidates = _DIGIT_RE.findall(cleaned)
    if candidates:
        try:
            universe_id = int(candidates[0])
            return await _handle_analyze_path(universe_id)
        except ValueError:
            pass

    return await _handle_freeform_path(cleaned, message.channel.id)
