"""Gemini AI Analyst v2 — Acquisition-focused opportunity analysis.

Analyzes *why* a game is a good acquisition opportunity.
Called as part of the alert pipeline, not just on demand.
Every call is cached in `ai_analysis_cache` for 24h.
"""

import json
import os
import sqlite3
import time
import requests

from config import DATABASE_PATH

DATABASE = DATABASE_PATH
CACHE_TTL_SECONDS = 86400  # 24h
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_DAILY_LIMIT_ENV = "GEMINI_DAILY_LIMIT"
DEFAULT_DAILY_LIMIT = 50
GEMINI_MODEL = "gemini-flash-latest"


VERDICT_QUOTA = "⏸️ Daily Quota Reached"
VERDICT_WORTH = "🟢 Worth Monitoring"
VERDICT_WAIT = "🟡 Wait for More Data"
VERDICT_RISKY = "🟠 Risky"
VERDICT_AVOID = "🔴 Avoid"
VERDICT_FALLBACK = "🟡 Data Only"

VALID_VERDICTS = {
    VERDICT_WORTH, VERDICT_WAIT, VERDICT_RISKY,
    VERDICT_AVOID, VERDICT_FALLBACK, VERDICT_QUOTA,
}


def _ensure_table():
    """Create ai_analysis_cache and gemini_usage tables if missing."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_analysis_cache (
        game_id INTEGER PRIMARY KEY,
        verdict TEXT,
        confidence INTEGER,
        strengths TEXT,
        risks TEXT,
        recommendation TEXT,
        raw TEXT,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gemini_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER,
        verdict TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def get_daily_limit():
    """Read GEMINI_DAILY_LIMIT from env; default is 50."""
    raw = os.environ.get(GEMINI_DAILY_LIMIT_ENV)
    try:
        value = int(raw)
        return max(0, value)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT


def get_usage_today():
    """Return the number of Gemini calls made today (UTC)."""
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT COUNT(*) FROM gemini_usage
    WHERE date(timestamp) = date('now')
    """)
    row = cursor.fetchone()
    conn.close()
    return int(row[0] if row else 0)


def quota_remaining():
    """Daily limit minus today's usage. Capped at 0."""
    return max(0, get_daily_limit() - get_usage_today())


def record_gemini_call(game_id, verdict):
    """Append a row to gemini_usage (idempotent table, append-only)."""
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO gemini_usage (game_id, verdict) VALUES (?, ?)",
        (int(game_id), verdict or ""),
    )
    conn.commit()
    conn.close()


def _cache_get(game_id):
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT verdict, confidence, strengths, risks, recommendation,
           raw, strftime('%s', last_checked)
    FROM ai_analysis_cache
    WHERE game_id = ?
    """, (int(game_id),))
    row = cursor.fetchone()
    conn.close()
    return row


def _cache_put(game_id, payload):
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO ai_analysis_cache
    (game_id, verdict, confidence, strengths, risks, recommendation, raw)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(game_id) DO UPDATE SET
        verdict=excluded.verdict,
        confidence=excluded.confidence,
        strengths=excluded.strengths,
        risks=excluded.risks,
        recommendation=excluded.recommendation,
        raw=excluded.raw,
        last_checked=CURRENT_TIMESTAMP
    """, (
        int(game_id),
        payload.get("verdict"),
        payload.get("confidence"),
        json.dumps(payload.get("strengths") or []),
        json.dumps(payload.get("risks") or []),
        payload.get("recommendation"),
        payload.get("raw") or "",
    ))
    conn.commit()
    conn.close()


def build_prompt_input(game_data):
    """Build a structured payload dict for acquisition analysis."""
    scout = game_data.get("scout_score") or {}
    breakdown = scout.get("breakdown") or []
    by_label = {label: pts for label, pts, _ in breakdown}

    return {
        "game_name": game_data.get("name", "Unknown"),
        "universe_id": game_data.get("id"),
        "playing": game_data.get("playing", 0),
        "visits": game_data.get("visits", 0),
        "favorites": game_data.get("favorites", 0),
        "growth_pct": game_data.get("growth", 0),
        "rating_percent": game_data.get("rating_percent", 0),
        "creator": game_data.get("creator", "Unknown"),
        "genre": game_data.get("genre", "Unknown"),
        "source": game_data.get("source", "Unknown"),
        "discord_invite": game_data.get("discord_invite", ""),
        "scout_score": scout.get("total", 0),
        "scout_verdict": scout.get("verdict", ""),
        "score_breakdown": {
            "growth": by_label.get("📈 Growth", 0),
            "momentum": by_label.get("👥 Player momentum", 0),
            "release": by_label.get("🆕 New release", 0),
            "likes": by_label.get("❤️ Like ratio", 0),
            "velocity": by_label.get("🚀 Velocity", 0),
            "developer": by_label.get("👨\u200d💻 Developer history", 0),
        },
        "trend_score": game_data.get("trend_score", 0),
    }


# Acquisition-focused prompt that explains why a game is a good opportunity
ACQUISITION_PROMPT_TEMPLATE = (
    "You are a Roblox game acquisition analyst. Your job is to assess "
    "whether a Roblox game is worth acquiring or investing in.\n\n"
    "Game data:\n{game_data}\n\n"
    "Return ONLY valid JSON matching this exact schema:\n"
    "{{\n"
    '  "verdict": "' + VERDICT_WORTH + ' | ' + VERDICT_WAIT + ' | '
    + VERDICT_RISKY + ' | ' + VERDICT_AVOID + '",\n'
    '  "confidence": <integer 0-100>,\n'
    '  "strengths": [ "<short bullet explaining why this is a good acquisition>", "<short bullet>" ],\n'
    '  "risks": [ "<short bullet explaining acquisition risks>", "<short bullet>" ],\n'
    '  "recommendation": "<one or two sentence actionable acquisition advice>"\n'
    "}}\n\n"
    "Focus on acquisition potential: audience size, growth trajectory, "
    "engagement metrics, creator reputation, monetization potential, "
    "and market fit. Be concise and data-driven. No prose. No markdown. Only JSON."
)


def _call_gemini(prompt):
    """Hit Gemini's generateContent endpoint. Returns dict or None on failure."""
    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            data = r.json()
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            return json.loads(text)
    except Exception:
        pass
    return None


def _fallback(game_data):
    """Deterministic explanation when Gemini is unavailable."""
    score = (game_data.get("scout_score") or {}).get("total", 0)
    return {
        "verdict": VERDICT_FALLBACK,
        "confidence": 0,
        "strengths": [
            f"Scout Score = {score}/100 (AI analysis not enabled).",
            f"{game_data.get('playing', 0):,} concurrent players.",
        ],
        "risks": [
            "AI analysis not enabled. Set GEMINI_API_KEY in environment to enable detailed acquisition assessment.",
        ],
        "recommendation": (
            "Add GEMINI_API_KEY to environment, restart the bot, "
            "and rerun for AI-powered acquisition context."
        ),
        "raw": "",
    }


def _quota_blocked_result(game_data):
    """Soft response when daily quota is exhausted."""
    score = (game_data.get("scout_score") or {}).get("total", 0)
    return {
        "verdict": VERDICT_QUOTA,
        "confidence": 0,
        "strengths": [
            f"Scout Score = {score}/100 (cached locally).",
        ],
        "risks": [
            "Daily Gemini quota exhausted — AI analysis paused today.",
        ],
        "recommendation": (
            "Lower GEMINI_DAILY_LIMIT to be safe; raise later once costs are "
            "clear. Live Gemini resumes at midnight UTC."
        ),
        "raw": "",
    }


def _normalize(result):
    """Force the result into our schema; replace bad values with safe defaults."""
    norm = {
        "verdict": result.get("verdict"),
        "confidence": result.get("confidence"),
        "strengths": result.get("strengths"),
        "risks": result.get("risks"),
        "recommendation": result.get("recommendation"),
        "raw": "",
    }

    if norm["verdict"] not in VALID_VERDICTS:
        norm["verdict"] = VERDICT_FALLBACK
    try:
        norm["confidence"] = max(0, min(100, int(norm["confidence"])))
    except Exception:
        norm["confidence"] = 0

    if not isinstance(norm["strengths"], list):
        norm["strengths"] = [str(norm["strengths"])] if norm["strengths"] else []
    if not isinstance(norm["risks"], list):
        norm["risks"] = [str(norm["risks"])] if norm["risks"] else []
    if not isinstance(norm["recommendation"], str):
        norm["recommendation"] = str(norm["recommendation"] or "")

    return norm


def analyze_game(game_data, force_refresh=False):
    """Return a structured acquisition analysis for a game.

    Cached per-universe-id for 24h. Falls back gracefully when Gemini is
    unreachable. Always returns a dict with `verdict`, `confidence`,
    `strengths`, `risks`, `recommendation`.
    """
    game_id = game_data.get("id")
    if not game_id:
        return _normalize({
            "verdict": VERDICT_FALLBACK,
            "confidence": 0,
            "strengths": [],
            "risks": ["Missing universe id"],
            "recommendation": "Cannot analyze — id missing.",
        })

    if not force_refresh:
        cached = _cache_get(game_id)
        if cached:
            last_ts = int(cached[6] or 0)
            if (time.time() - last_ts) < CACHE_TTL_SECONDS:
                return _normalize({
                    "verdict": cached[0],
                    "confidence": int(cached[1] or 0),
                    "strengths": json.loads(cached[2] or "[]"),
                    "risks": json.loads(cached[3] or "[]"),
                    "recommendation": cached[4] or "",
                    "raw": cached[5] or "",
                })

    # Daily-quota gate: refuse live Gemini before making a request.
    if quota_remaining() <= 0:
        return _normalize(_quota_blocked_result(game_data))

    payload = build_prompt_input(game_data)
    prompt = ACQUISITION_PROMPT_TEMPLATE.format(
        game_data=json.dumps(payload, indent=2)
    )
    raw_result = _call_gemini(prompt)

    if raw_result is None:
        normalized = _normalize(_fallback(game_data))
    else:
        normalized = _normalize(raw_result)
        normalized["raw"] = json.dumps(raw_result)
        record_gemini_call(game_id, normalized.get("verdict"))

    _cache_put(game_id, normalized)
    return normalized


# ---- freeform assistant helpers (unchanged) -----------------------------------------

import hashlib


QUESTION_CACHE_TTL_SECONDS = 86400
QUESTION_PROMPT_TEMPLATE = (
    "You are a Roblox scouting assistant. The user is asking about Roblox "
    "games, scouting, acquisitions, developer outreach, or related "
    "scouting topics. Answer concisely.\n\n"
    "User question:\n{question}\n\n"
    "If the question is about a specific game ID, defer to the analyze_game "
    "pipeline which produces a structured report — this assistant tracks "
    "freeform questions.\n\n"
    "Respond in markdown. Prefer bullet lists for clarity."
)

QUESTION_PROMPT_TEMPLATE_CONVERSATIONAL = (
    "You are a Roblox scouting assistant talking to a user in a Discord "
    "#ai-scout channel. Use the recent conversation history for context — "
    "if the user says \"that game\" or \"the one with 50k players\", you "
    "should recall the universe id or name from the history.\n\n"
    "Recent conversation (oldest first):\n"
    "{history}\n\n"
    "Latest user message:\n"
    "{question}\n\n"
    "If the latest message includes a Roblox Universe ID, you may describe "
    "the game briefly but defer to the structured analyze_game report that "
    "the bot will generate separately.\n\n"
    "Respond in markdown. Prefer short paragraphs, then bullet lists. "
    "Do not restate the question. Answer directly."
)


def _ensure_question_table():
    """Create ai_question_cache if missing."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_question_cache (
        key TEXT PRIMARY KEY,
        answer TEXT,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def _question_cache_get(key):
    """Return (answer, last_checked_epoch) or None."""
    _ensure_question_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT answer, strftime('%s', last_checked)
    FROM ai_question_cache WHERE key = ?
    """, (key,))
    row = cursor.fetchone()
    conn.close()
    return row


def _question_cache_put(key, answer):
    """Upsert the cached answer for a question-hash key."""
    _ensure_question_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO ai_question_cache (key, answer) VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET
        answer=excluded.answer,
        last_checked=CURRENT_TIMESTAMP
    """, (key, answer))
    conn.commit()
    conn.close()


def _question_cache_valid(row, now_epoch):
    return bool(row) and (now_epoch - int(row[1] or 0)) < QUESTION_CACHE_TTL_SECONDS


def answer_question(question, force_refresh=False, history=None):
    """Send a freeform question to Gemini. Cached by question hash 24h."""
    question = (question or "").strip()
    if not question:
        return "🤖 I'm listening — try `@Roblox Game Scout <game id>` or a question."

    key = hashlib.sha1(question.lower().encode()).hexdigest()

    if not force_refresh:
        cached = _question_cache_get(key)
        if cached and _question_cache_valid(cached, time.time()):
            return cached[0] or "(cached answer was empty)"

    if quota_remaining() <= 0:
        return (
            "⏸️ Daily Gemini quota exhausted — assistant paused until "
            "midnight UTC."
        )

    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        return (
            "ℹ️ AI assistant not configured yet — set `GEMINI_API_KEY` in "
            "Replit Secrets, then mention me again."
        )

    if history:
        history_text = "\n".join(f"{role}: {content}" for role, content in history)
        prompt = QUESTION_PROMPT_TEMPLATE_CONVERSATIONAL.format(
            history=history_text, question=question
        )
    else:
        prompt = QUESTION_PROMPT_TEMPLATE.format(question=question)

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    try:
        r = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            answer = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            ).strip()
            if not answer:
                answer = "(Gemini returned an empty response.)"
        else:
            if r.status_code == 429:
                answer = (
                    "⚠️ Gemini is rate-limited right now (HTTP 429). "
                    "Try again in a minute, or wait for the next daily quota window."
                )
            else:
                answer = f"⚠️ Gemini returned HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        answer = f"⚠️ Gemini call failed: `{exc}`"

    _question_cache_put(key, answer)
    record_gemini_call(0, "📝 Assistant answer")
    return answer


def format_report(name, analysis):
    """Render a Discord-friendly markdown report from a structured analysis."""
    verdict = analysis.get("verdict", "Unknown")
    confidence = analysis.get("confidence", 0)
    strengths = analysis.get("strengths") or []
    risks = analysis.get("risks") or []
    recommendation = analysis.get("recommendation") or "—"

    strengths_text = "\n".join(f"✅ {s}" for s in strengths) or "—"
    risks_text = "\n".join(f"⚠️ {r}" for r in risks) or "—"

    return (
        f"🤖 **AI Acquisition Report**\n\n"
        f"**Game:** {name}\n\n"
        f"**Verdict:** {verdict}\n\n"
        f"**Confidence:** {confidence}%\n\n"
        f"**Why this is a good acquisition:**\n{strengths_text}\n\n"
        f"**Risks to consider:**\n{risks_text}\n\n"
        f"**Recommendation:** {recommendation}"
    )