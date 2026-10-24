"""AI Analyst v3 — Acquisition-focused opportunity analysis.

Analyzes *why* a game is a good acquisition opportunity.
Called as part of the alert pipeline, not just on demand.
Every call is cached in `ai_analysis_cache` for 24h.

Provider selection (config.AI_PROVIDER):
  - "auto"    (default): Groq (GROQ_API_KEY) if set, else Gemini (GEMINI_API_KEY).
  - "groq" / "gemini": force one provider.

Groq is a free, OpenAI-compatible inference API (https://groq.com) — no
credit card, far higher daily limits than Gemini's free tier. The endpoint,
model, and key are all configurable (AI_BASE_URL / AI_MODEL) so any
OpenAI-compatible host (Groq, Cerebras, OpenRouter, ...) works unmodified.
"""

import json
import os
import sqlite3
import time
import requests

from config import DATABASE_PATH, AI_PROVIDER, AI_BASE_URL, AI_MODEL

DATABASE = DATABASE_PATH
CACHE_TTL_SECONDS = 86400  # 24h
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GROQ_API_KEY_ENV = "GROQ_API_KEY"
GEMINI_DAILY_LIMIT_ENV = "GEMINI_DAILY_LIMIT"
DEFAULT_DAILY_LIMIT = 50
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

# Tried in order when the primary model keeps returning a retryable status
# (429/503). Measured on a free-tier key: gemini-flash-latest returned
# 0/4 (three 429s and a "high demand" 503) while every flash-lite alias
# returned 4/4 at ~1s. The flash tier is simply too contended to depend on,
# so the default model is flash-lite and these are the safety net if even
# that is busy.
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.1-flash-lite,gemini-3.5-flash-lite",
    ).split(",")
    if m.strip()
]

# Status codes that mean "come back in a moment", not "you're wrong".
# Gemini in particular returns 503 UNAVAILABLE with "This model is
# currently experiencing high demand" on a large share of calls right
# now — measured at 3 of 4 in testing — and without a retry every one of
# those games silently degrades to the "Data Only" fallback verdict.
# 401/403/404 are deliberately excluded: those are real config errors and
# retrying them just burns time.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AI_MAX_ATTEMPTS = int(os.environ.get("AI_MAX_ATTEMPTS", "4"))


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn


def _post_with_retry(url, payload, headers=None, timeout=30, attempts=None):
    """POST JSON, retrying transient upstream failures with backoff.

    Returns (response, None) on success, or (None, error_string) if every
    attempt failed. A response is returned for any non-retryable status so
    the caller can still distinguish 401/403/404 from an overload.
    """
    import random

    attempts = attempts or AI_MAX_ATTEMPTS
    backoff = 1.0
    last_note = "request failed"
    for attempt in range(1, max(1, attempts) + 1):
        try:
            r = requests.post(url, json=payload, headers=headers or {}, timeout=timeout)
        except Exception as exc:
            last_note = f"request failed: {exc}"
        else:
            if r.status_code not in _RETRYABLE_STATUS:
                return r, None
            last_note = f"HTTP {r.status_code}"
            if attempt >= attempts:
                return r, None
            wait = backoff + random.uniform(0, 0.4)
            time.sleep(wait)
            backoff = min(backoff * 2, 8.0)
            continue
        # Network-level failure (no response object).
        if attempt >= attempts:
            break
        wait = backoff
        time.sleep(wait)
        backoff = min(backoff * 2, 8.0)
    return None, last_note


# Why the last AI call returned None. Purely diagnostic — surfaced in
# the fallback text so operators see the real cause instead of a generic
# "key not configured" message.
_LAST_AI_ERROR = None

# Which provider actually answered the last successful call, or None if the
# last call failed. Surfaced in the logs so "which provider is actually
# serving requests right now" is answerable at a glance.
_LAST_AI_PROVIDER = None


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
    conn = _connect(DATABASE)
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
    conn = _connect(DATABASE)
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
    conn = _connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO gemini_usage (game_id, verdict) VALUES (?, ?)",
        (int(game_id), verdict or ""),
    )
    conn.commit()
    conn.close()


def _cache_get(game_id):
    _ensure_table()
    conn = _connect(DATABASE)
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
    conn = _connect(DATABASE)
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


def _active_provider():
    """Resolve which AI provider to use: 'groq', 'gemini', or None."""
    chain = _provider_chain()
    return chain[0] if chain else None


def _provider_chain():
    """Providers to try, in order, for one AI request.

    With both keys set, Groq answers first and Gemini is the backup. The
    point of the backup is that free tiers go down constantly — a
    permanently-preferred provider with no escape hatch means one
    provider's bad day becomes the bot's permanent "Data Only" verdicts.
    An explicit AI_PROVIDER=groq|gemini still means exactly that provider
    and nothing else, so a deliberate pin is never second-guessed.
    """
    # Read the selector from the environment at call time so changing
    # AI_PROVIDER doesn't require a code reload (config only samples it once).
    provider = (os.environ.get("AI_PROVIDER") or AI_PROVIDER or "auto").lower()
    if provider in ("groq", "gemini"):
        key_env = GROQ_API_KEY_ENV if provider == "groq" else GEMINI_API_KEY_ENV
        return [provider] if os.environ.get(key_env) else []
    chain = []
    if os.environ.get(GROQ_API_KEY_ENV):
        chain.append("groq")
    if os.environ.get(GEMINI_API_KEY_ENV):
        chain.append("gemini")
    return chain


def _provider_model(provider):
    return AI_MODEL if provider == "groq" else GEMINI_MODEL


def _provider_label(provider):
    return "Groq" if provider == "groq" else "Gemini"


def provider_status():
    """Diagnostics: which providers are configured and in what order."""
    chain = _provider_chain()
    if not chain:
        return "no provider configured — set GROQ_API_KEY or GEMINI_API_KEY"
    # Read the same live value _provider_chain() uses. Reading the
    # import-time AI_PROVIDER constant here made /status claim
    # "auto-selected" even when AI_PROVIDER was pinned at runtime.
    forced = (
        os.environ.get("AI_PROVIDER") or AI_PROVIDER or "auto"
    ).lower() in ("groq", "gemini")
    parts = [f"{_provider_label(p)} ({_provider_model(p)})" for p in chain]
    if len(parts) > 1:
        return " | ".join(parts) + " | primary, backup auto-selected"
    return f"{parts[0]} | " + (
        "forced by AI_PROVIDER" if forced else "auto-selected"
    )


def _call_openai_compatible(messages, api_key):
    """POST to an OpenAI-compatible chat/completions endpoint (Groq etc.).

    Returns the parsed JSON dict the model produced, or None on failure
    (with _LAST_AI_ERROR describing the cause).
    """
    global _LAST_AI_ERROR
    _LAST_AI_ERROR = None

    url = f"{AI_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }
    r, transport_error = _post_with_retry(
        url,
        payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if r is None:
        _LAST_AI_ERROR = transport_error
        return None
    if r.status_code == 200:
        data = r.json()
        text = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _LAST_AI_ERROR = "model response was not valid JSON"
            return None
    if r.status_code == 404:
        _LAST_AI_ERROR = (
            f"model '{AI_MODEL}' not found at {AI_BASE_URL} (HTTP 404) — "
            f"check AI_MODEL"
        )
    elif r.status_code in (401, 403):
        _LAST_AI_ERROR = f"API key rejected (HTTP {r.status_code})"
    elif r.status_code == 429:
        _LAST_AI_ERROR = "rate limited (HTTP 429)"
    else:
        _LAST_AI_ERROR = f"HTTP {r.status_code} after retries"
    return None


def _call_gemini(prompt):
    """Hit Gemini's generateContent endpoint. Returns dict or None on failure.

    Distinguishes failure causes so callers can report them accurately —
    a model-name 404 used to masquerade as "key not configured" because
    every failure fell into the same generic fallback text (see
    .agents/memory/gemini-model-404-misdiagnosis.md).

    If the primary model is throttled or overloaded, walks the fallback
    model list before giving up, so a busy model degrades into a slightly
    slower answer rather than into the "Data Only" fallback verdict.
    """
    global _LAST_AI_ERROR
    _LAST_AI_ERROR = None  # reset so stale errors never leak through

    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        _LAST_AI_ERROR = "not_configured"
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            # Explicit budget: current Gemini aliases resolve to thinking
            # models, and thinking tokens come out of the same allowance.
            # Left unset, a long analysis can spend the whole default
            # budget on thinking and come back with no text at all.
            "maxOutputTokens": 4096,
        },
    }

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    last_note = None
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        r, transport_error = _post_with_retry(url, payload, timeout=45)
        if r is None:
            last_note = transport_error
            continue
        if r.status_code == 200:
            parsed = _parse_gemini_json(r)
            if parsed is not None:
                if model != GEMINI_MODEL:
                    _LAST_AI_ERROR = None
                return parsed
            last_note = "model returned unparseable JSON"
            continue
        # A 404 means the model name is wrong or retired for this key —
        # trying the next candidate is still useful, but a 401/403 is a
        # key problem that no other model will fix.
        last_note = f"HTTP {r.status_code} from {model}"
        if r.status_code in (401, 403):
            _LAST_AI_ERROR = f"API key rejected (HTTP {r.status_code})"
            return None

    _LAST_AI_ERROR = f"{last_note} (tried {len(models)} model(s))"
    return None


def _parse_gemini_json(r):
    """Pull the JSON object out of a 200 response, or None if absent."""
    data = r.json()
    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None  # unparseable JSON — treat as upstream failure


def _call_gemini_single_model(prompt, model):
    """One model, no fallback walk. Kept for tests/diagnostics."""
    global _LAST_AI_ERROR
    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    r, transport_error = _post_with_retry(
        url,
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": 4096,
            },
        },
        timeout=45,
    )
    if r is None:
        _LAST_AI_ERROR = transport_error
        return None
    if r.status_code == 200:
        return _parse_gemini_json(r)
    _LAST_AI_ERROR = f"HTTP {r.status_code} from {model}"
    return None


def _call_model(prompt):
    """Dispatch a structured-JSON prompt to the configured AI provider(s).

    Walks the provider chain, so a throttled or rejected primary hands off
    to the backup instead of collapsing to the "Data Only" verdict. Returns
    the parsed dict or None if every configured provider failed.
    """
    global _LAST_AI_ERROR, _LAST_AI_PROVIDER
    chain = _provider_chain()
    if not chain:
        _LAST_AI_ERROR = "not_configured"
        return None

    attempts = []
    for provider in chain:
        if provider == "groq":
            result = _call_openai_compatible(
                [{"role": "user", "content": prompt}],
                os.environ.get(GROQ_API_KEY_ENV),
            )
        else:
            result = _call_gemini(prompt)
        if result is not None:
            _LAST_AI_PROVIDER = provider
            return result
        attempts.append(f"{_provider_label(provider)}: {_LAST_AI_ERROR}")

    _LAST_AI_PROVIDER = None
    _LAST_AI_ERROR = "; ".join(attempts)
    return None


def _fallback(game_data):
    """Deterministic explanation when the AI provider is unavailable."""
    score = (game_data.get("scout_score") or {}).get("total", 0)
    if _LAST_AI_ERROR == "not_configured":
        risks = [
            "AI analysis not enabled. Set GROQ_API_KEY (free, recommended) "
            "or GEMINI_API_KEY in the environment to enable detailed "
            "acquisition assessment.",
        ]
        recommendation = (
            "Add GROQ_API_KEY or GEMINI_API_KEY to the environment, restart "
            "the bot, and rerun for AI-powered acquisition context."
        )
    else:
        # A key IS configured but the call failed — say why so a broken
        # model name or rejected key isn't misread as "key not configured".
        risks = [
            f"AI analysis unavailable — last AI call failed: {_LAST_AI_ERROR}.",
        ]
        recommendation = (
            "Data-only verdict below. Check the bot logs for the AI "
            "error and verify the API key and AI_MODEL setting."
        )
    return {
        "verdict": VERDICT_FALLBACK,
        "confidence": 0,
        "strengths": [
            f"Scout Score = {score}/100 (AI analysis not enabled).",
            f"{game_data.get('playing', 0):,} concurrent players.",
        ],
        "risks": risks,
        "recommendation": recommendation,
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
            "Daily AI quota exhausted — analysis paused until midnight UTC.",
        ],
        "recommendation": (
            "Lower GEMINI_DAILY_LIMIT to be safe; raise later once costs are "
            "clear. Live AI analysis resumes at midnight UTC."
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
    raw_result = _call_model(prompt)

    if raw_result is None:
        # Do NOT cache fallback/quota results: they'd freeze today's outage
        # (missing key, 404, exhausted quota) into the 24h cache and the
        # game would never get a real AI analysis until the cache expired.
        return _normalize(_fallback(game_data))

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
    conn = _connect(DATABASE)
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
    conn = _connect(DATABASE)
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
    conn = _connect(DATABASE)
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


def _call_provider_text(prompt):
    """Send a freeform (non-JSON) prompt, walking the provider chain.

    Returns (answer_text, error_note). error_note is None on success.
    """
    chain = _provider_chain()
    if not chain:
        return None, "not_configured"
    notes = []
    for provider in chain:
        answer, note = _call_provider_text_single(prompt, provider)
        if answer:
            return answer, None
        notes.append(note or f"{_provider_label(provider)} failed")
    return None, " | ".join(notes)


def _call_provider_text_single(prompt, provider):
    """One provider, no fallback. Returns (answer_text, error_note)."""
    if provider == "groq":
        url = f"{AI_BASE_URL.rstrip('/')}/chat/completions"
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
        }
        headers = {
            "Authorization": f"Bearer {os.environ.get(GROQ_API_KEY_ENV)}",
            "Content-Type": "application/json",
        }
        label = "AI"
    else:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={os.environ.get(GEMINI_API_KEY_ENV)}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        headers = {}
        label = "Gemini"

    r, transport_error = _post_with_retry(url, payload, headers=headers, timeout=30)
    if r is None:
        return None, f"⚠️ {label} call failed: `{transport_error}`"
    if r.status_code == 200:
        data = r.json()
        if provider == "groq":
            answer = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        else:
            answer = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
        answer = (answer or "").strip()
        if answer:
            return answer, None
        return None, f"{label} returned an empty response."
    if r.status_code == 429:
        return None, (
            f"⚠️ {label} is rate-limited right now (HTTP 429). "
            "Try again in a minute, or wait for the next daily quota window."
        )
    return None, f"⚠️ {label} returned HTTP {r.status_code}: {r.text[:200]}"


def answer_question(question, force_refresh=False, history=None):
    """Send a freeform question to the active AI provider. Cached 24h."""
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
            "⏸️ Daily AI quota exhausted — assistant paused until midnight UTC."
        )

    if _active_provider() is None:
        return (
            "ℹ️ AI assistant not configured yet — set `GROQ_API_KEY` (free, "
            "recommended) or `GEMINI_API_KEY` in the environment, then "
            "mention me again."
        )

    if history:
        history_text = "\n".join(f"{role}: {content}" for role, content in history)
        prompt = QUESTION_PROMPT_TEMPLATE_CONVERSATIONAL.format(
            history=history_text, question=question
        )
    else:
        prompt = QUESTION_PROMPT_TEMPLATE.format(question=question)

    answer, error = _call_provider_text(prompt)
    if answer is None:
        return error  # do not cache failures (same rule as analyze_game)

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