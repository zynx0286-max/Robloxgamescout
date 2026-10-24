"""
Ledger — the accountability spine, made continuous.

Wraps scorecard.py with the pieces the bot needs to publish its own
performance weekly (unasked), and extends grading to its **AI verdicts**
specifically, since those consume the bot's daily AI quota and deserve
their own record:

* `grade_ai_call`: a bullish "Worth Monitoring" verdict is graded how the
  scorecard grades any alert (grow ≥ +20% = correct, fall ≤ −20% =
  otherwise). Bearish verdicts (Risky/Avoid) invert the rule — the AI was
  right when the game did *not* grow. Neutral verdicts are ever graded.
* `build_weekly_report()` + `weekly_report_text()`: a staged, digest-
  friendly weekly report assembled from pure local data.
* `weekly_ledger_body(bot)`: the scheduler-loop body that posts the
  report once per ISO week at/after the configured weekday+hour — keyed
  by ISO year-week so a mid-week restart cannot double-post and a Monday
  outage catch-up still lands later in the same week.

Anti-double-posting, week-key catch-up, channel-resolution guards and
fail-soft behavior are all covered by tests/test_ledger.py.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import DATABASE_PATH
from scorecard import (
    collect_calls,
    evaluate_calls,
    summarize,


    render_summary,
    render_calls,
)

logger = logging.getLogger("ledger")


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn

# AI-verdict strings (mirrors gemini_analyzer's constants; kept local to
# avoid a circular import through the AI module).
BULLISH_AI_VERDICTS = {"🟢 Worth Monitoring"}
BEARISH_AI_VERDICTS = {"🟠 Risky", "🔴 Avoid"}
NEUTRAL_AI_VERDICTS = {"🟡 Wait for More Data", "🟡 Data Only", "⏸️ Daily Quota Reached"}


# ---------------------------------------------------------------------------
# AI verdict grading
# ---------------------------------------------------------------------------

def classify_verdict(verdict: Optional[str]) -> str:
    """Map an AI verdict string to bullish / bearish / neutral / unknown."""
    if not verdict:
        return "unknown"
    v = str(verdict).strip()
    if v in BULLISH_AI_VERDICTS:
        return "bullish"
    if v in BEARISH_AI_VERDICTS:
        return "bearish"
    if v in NEUTRAL_AI_VERDICTS:
        return "neutral"
    return "unknown"


def grade_ai_call(verdict: str, delta_pct: Optional[float]) -> str:
    """Grade one AI prediction against the measured outcome.

    Bullish verdicts are right when the game grew ≥ +20%;
    bearish verdicts are right when it fell ≤ −20% (the AI said don't,
    so a declining game confirms the call). Neutral/unknown verdicts are
    not graded. Returns one of: correct / incorrect / ungraded /
    insufficient_data.
    """
    stance = classify_verdict(verdict)
    if stance in ("neutral", "unknown"):
        return "ungraded"
    if delta_pct is None:
        return "insufficient_data"
    if stance == "bullish":
        return "correct" if delta_pct >= 20.0 else "incorrect"
    return "correct" if delta_pct <= -20.0 else "incorrect"


# ---------------------------------------------------------------------------
# Weekly-report assembly (pure over staged inputs)
# ---------------------------------------------------------------------------

def build_weekly_report(
    scorecard_summary: Optional[dict] = None,
    scorecard_summary_text: Optional[str] = None,
    top_calls: Optional[list] = None,
    top_calls_lines: Optional[list] = None,
    ai_stats: Optional[dict] = None,
    ai_lines: Optional[list] = None,
) -> str:
    """Compose the weekly message from pre-staged inputs.

    Every part is optional; each renders an explicit quiet line when
    absent, never a blank section.
    """
    lines = ["📜 **Weekly Ledger — the bot's own report card**"]
    lines.append("")

    # Alert calls at a glance.
    if scorecard_summary_text:
        lines.append("**Alert calls:**")
        lines.append(scorecard_summary_text)
    elif scorecard_summary is not None:
        lines.append("**Alert calls:**")
        lines.append(render_summary(scorecard_summary))
    else:
        lines.append("**Alert calls:** _No data this window._")

    # Notable calls (biggest movers among the graded).
    if top_calls_lines:
        lines.append("")
        lines.append("**Notable calls:**")
        lines.extend(top_calls_lines)

    # AI verdict grading.
    if ai_lines:
        lines.append("")
        lines.append("**AI verdicts:**")
        lines.extend(ai_lines)
    elif ai_stats is not None:
        lines.append("")
        lines.append("**AI verdicts:**")
        _render_ai_stats(ai_stats, lines)

    return "\n".join(lines)


def _render_ai_stats(stats: dict, lines: list) -> None:
    """Append AI-verdict stat lines."""
    if not stats.get("rateable"):
        lines.append("_No AI verdicts were graded this window._")
        return
    acc = stats["correct"] / stats["rateable"] * 100.0
    parts = [f"{stats['correct']} correct", f"{stats['incorrect']} incorrect"]
    if stats.get("ungraded"):
        parts.append(f"{stats['ungraded']} ungraded")
    lines.append(f"{acc:.0f}% of graded AI verdicts were correct ({', '.join(parts)})")


def summarize_ai(graded: list) -> dict:
    """Aggregate AI-verdict grades. Pure."""
    rateable = [g for g in graded if g["grade"] in ("correct", "incorrect")]
    return {
        "total": len(graded),
        "rateable": len(rateable),
        "correct": sum(1 for g in rateable if g["grade"] == "correct"),
        "incorrect": sum(1 for g in rateable if g["grade"] == "incorrect"),
        "ungraded": len(graded) - len(rateable),
    }


# ---------------------------------------------------------------------------
# Staged orchestration — pure local data, one flat read pass
# ---------------------------------------------------------------------------

def stage_weekly_data(
    db_path: Optional[str] = None,
    lookback_hours: int = 168,
) -> dict:
    """Assemble the inputs for the weekly report.

    Scorecard calls are graded over the scorecard's own 72h window
    (compact and channel-friendly); AI verdicts are graded over the full
    week so the report is a true weekly record. Never touches the
    network.
    """
    db_path = db_path or DATABASE_PATH

    # --- Part 1: scorecard ---------------------------------------------------
    calls = evaluate_calls(collect_calls(db_path, lookback_hours=72))
    summary = summarize(calls)

    # --- Part 2: AI verdict grades -------------------------------------------
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT gu.game_id, gu.verdict, gu.timestamp
            FROM gemini_usage gu
            JOIN alerted_games ag ON ag.game_id = gu.game_id
            WHERE gu.verdict IS NOT NULL
              AND gu.timestamp >= datetime('now', ?)
              AND ag.first_alerted IS NOT NULL
            ORDER BY gu.timestamp DESC
            LIMIT 200
            """,
            (f"-{lookback_hours} hours",),
        ).fetchall()
    finally:
        conn.close()

    graded_calls_by_id = {
        c["game_id"]: c for c in calls if c.get("delta_pct") is not None
    }
    graded_ai: list = []
    for game_id, verdict, _ts in rows:
        call = graded_calls_by_id.get(int(game_id))
        if call is None:
            # Only games the scorecard could measure (a comparable
            # baseline-to-latest pair on file) contribute their AI
            # verdict this week; unmeasurable calls stay out entirely
            # rather than padding the record with noise rows.
            continue
        graded_ai.append({
            "game_id": int(game_id),
            "verdict": verdict,
            "grade": grade_ai_call(verdict, call.get("delta_pct")),
        })

    return {
        "calls": calls,
        "summary": summary,
        "graded_ai": graded_ai,
        "ai_stats": summarize_ai(graded_ai),
    }


def _featured_call_lines(calls: list, limit: int = 3) -> list:
    """Top-|delta| call lines for the Notable-calls block. Pure."""
    rateable = [c for c in calls if c.get("delta_pct") is not None]
    featured = sorted(rateable, key=lambda c: abs(c["delta_pct"]), reverse=True)
    return render_calls(featured, limit=limit)


def weekly_report_text(db_path: Optional[str] = None) -> str:
    """The weekly ledger text from the live database. Fail-soft: a data
    problem shrinks the report, never raises."""
    try:
        staged = stage_weekly_data(db_path)
    except Exception as exc:
        logger.warning("Weekly ledger data collection failed: %s", exc)
        summary = None
        staged = {"calls": [], "summary": None, "graded_ai": [], "ai_stats": None}
    try:
        return build_weekly_report(
            scorecard_summary_text=render_summary(staged["summary"]) if staged["summary"] is not None else None,
            top_calls_lines=_featured_call_lines(staged["calls"]),
            ai_stats=staged["ai_stats"],
        )
    except Exception as exc:
        logger.exception("Weekly ledger render failed: %s", exc)
        return build_weekly_report(scorecard_summary=None, ai_stats=None)


# ---------------------------------------------------------------------------
# Scheduler loop body + ISO-week anti-double-post
# ---------------------------------------------------------------------------

_last_ledger_post_week: Optional[str] = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_week_key(dt: Optional[datetime] = None) -> str:
    """ISO year-week key, e.g. 2026-W41. The identity of 'this week' for
    anti-double-posting and catch-up."""
    dt = dt or _now_utc()
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _iso_week_monday(now: datetime) -> datetime:
    """UTC midnight of the Monday opening the ISO week containing now."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.isoweekday() - 1)


def _due_moment(weekday: int, hour: int, now: datetime) -> datetime:
    """The exact due datetime inside the ISO week containing now:
    the configured weekday at the configured hour (UTC). Returns also
    whether that weekday lies in this ISO week or the next one (it sits
    in the next week only when the cadence weekday is *before* this ISO
    week's Monday — e.g. a Sunday-due bot viewed from the Monday–Sat
    portion of its cycle).
    """
    monday = _iso_week_monday(now)
    due = monday + timedelta(days=weekday - 1)
    due = due.replace(hour=hour)
    # A due day earlier in the week than Monday belongs to the previous
    # ISO week's cycle — add a week so it is the upcoming due moment.
    if due < monday:
        due += timedelta(days=7)
    return due


def _week_key_due(weekday: int, hour: int, now: datetime) -> str:
    """The ISO week key the upcoming publish belongs to — the ISO week
    containing the due moment (not necessarily the current one)."""
    return _iso_week_key(_due_moment(weekday, hour, now))


async def weekly_ledger_body(bot) -> bool:
    """Run the weekly publish check. Returns True when a report posted.

    Semantics:
    * The candidate post is for the week that started at the configured
      weekday. Before that weekday's hour, no post; on or after it, the
      post is due for the current (or an ongoing) week.
    * The last-posted week key is updated ONLY after a successful post,
      so a failed attempt is retried on the next hourly tick instead of
      silently eating the week.
    """
    global _last_ledger_post_week

    # Late import: the loop body needs config at call time, not import
    # time, so tests can patch cadence freely.
    from config import (
        ALERT_CHANNEL_ID,
        LEDGER_ENABLED,
        LEDGER_WEEKDAY,
        LEDGER_HOUR_UTC,
    )

    if not LEDGER_ENABLED:
        return False

    now = _now_utc()
    due_at = _due_moment(LEDGER_WEEKDAY, LEDGER_HOUR_UTC, now)
    # Before the due moment: wait (the tick holds until the due day+hour
    # arrives); at/after it: the post for the week containing the due
    # moment is due — even days later, as catch-up.
    if now < due_at:
        return False

    due_week = _week_key_due(LEDGER_WEEKDAY, LEDGER_HOUR_UTC, now)
    if _last_ledger_post_week == due_week:
        return False

    # Only post when the alert channel can actually receive it.
    channel = None
    try:
        channel = bot.get_channel(ALERT_CHANNEL_ID)
    except Exception:
        logger.exception("Ledger channel lookup failed")
    if channel is None:
        logger.warning(
            "Weekly ledger due but ALERT_CHANNEL_ID is unresolved; "
            "will retry next tick."
        )
        return False

    text = weekly_report_text()

    if getattr(channel, "send", None) is None:
        logger.warning("Weekly ledger: channel object has no send; skipping.")
        return False

    try:
        await channel.send(text)
    except Exception as exc:
        # Post failed: do NOT burn the week, so the next tick retries.
        logger.exception("Weekly ledger post failed (will retry): %s", exc)
        return False

    _last_ledger_post_week = due_week
    return True
