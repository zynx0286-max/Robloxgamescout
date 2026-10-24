"""
Scorecard — the bot grading its own calls.

The bot tells you which games *might* grow. The natural next question:
which of its own picks **did** grow? This module answers it from the
bot's own records: for every game marked in `alerted_games`, it compares
the snapshot recorded at alert time against the freshest snapshot, and
renders the outcome as a per-call verdict.

Nothing here touches the network. The alert baseline is exactly what the
bot measured at alert time, and the "now" is the freshest snapshot — so
the scorecard is free, instant, and honest (it grades against what the
bot actually recorded, not a later re-derivation).

The three readable verdicts:
  ✅ Win   — the call was right (CCU grew by at least SUCCESS_PCT)
  🔕 Quiet — the game didn't move meaningfully either way
  📉 Miss  — the game declined materially since the alert

Design constraints, enforced by tests:

* `evaluate_calls` and `render_scorecard` are pure over the data they
  take in, so tests verify behavior without a database.
* Games with only a zero-baseline snapshot are reported as "unrateable"
  (not as a miss or a win) — honest about what cannot be judged.
* The aggregate summary leads with the hit rate, not a vanity number.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import DATABASE_PATH


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn

logger = logging.getLogger("scorecard")

# Only games alerted within this many hours are graded; older calls are
# ancient history for a 30-day-old snapshot store.
LOOKBACK_HOURS = 72
# A CCU rise at or above this percentage since the alert counts as a win.
SUCCESS_PCT = 20.0
# A CCU fall at or below this counts as a material miss.
MISS_PCT = -20.0
# Cap on calls rendered so the channel output stays readable.
RENDER_LIMIT = 10


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def evaluate_call(alert_ts: str, baseline: int, latest: int) -> dict:
    """Grade one alerted game from its own numbers.

    Pure function. Returns {outcome, delta_pct} where outcome is one of
    "win" / "quiet" / "miss" / "unrateable".
    """
    if baseline is None or baseline <= 0 or latest is None or latest <= 0:
        return {"outcome": "unrateable", "delta_pct": None}
    delta_pct = (latest - baseline) / baseline * 100.0
    if delta_pct >= SUCCESS_PCT:
        outcome = "win"
    elif delta_pct <= MISS_PCT:
        outcome = "miss"
    else:
        outcome = "quiet"
    return {"outcome": outcome, "delta_pct": round(delta_pct, 1)}


def collect_calls(
    db_path: Optional[str] = None,
    lookback_hours: int = LOOKBACK_HOURS,
) -> list[dict]:
    """Alerted games within the lookback, with alert-time baseline and
    freshest snapshot, ready for grading.

    The baseline is the snapshot whose timestamp is the closest one at or
    before the alert timestamp (the bot's own measurement at alert time,
    not a later reconstruction). The "latest" reading is the freshest
    snapshot overall — identical to what every other signal lane reads.

    ONE query serves the whole batch: two correlated subqueries per row
    against `alerted_games`, both hitting the (game_id, timestamp)
    index. The per-game Python loop this replaces opened the same two
    lookups for every alerted game — and `ledger.py` grades its weekly
    report through this same path.
    """
    db_path = db_path or DATABASE_PATH
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT a.game_id, a.game_name, a.first_alerted,
                   (SELECT h.players FROM game_history h
                     WHERE h.game_id = a.game_id
                       AND h.timestamp <= a.first_alerted
                     ORDER BY h.timestamp DESC LIMIT 1) AS baseline,
                   (SELECT h.players FROM game_history h
                     WHERE h.game_id = a.game_id
                     ORDER BY h.timestamp DESC LIMIT 1) AS latest
            FROM alerted_games a
            WHERE a.first_alerted IS NOT NULL
              AND a.first_alerted >= datetime('now', ?)
            ORDER BY a.first_alerted DESC
            """,
            (f"-{lookback_hours} hours",),
        ).fetchall()
    finally:
        conn.close()

    calls: list[dict] = []
    for game_id, game_name, alert_ts, baseline, latest in rows:
        gid = int(game_id)
        calls.append({
            "game_id": gid,
            "game_name": game_name or f"Game {gid}",
            "alert_ts": str(alert_ts)[:19],
            "baseline": int(baseline) if baseline is not None else None,
            "latest": int(latest) if latest is not None else None,
        })
    return calls


def evaluate_calls(calls: list[dict]) -> list[dict]:
    """Attach verdicts to every call. Pure — takes data in, adds verdicts."""
    graded: list[dict] = []
    for c in calls:
        g = evaluate_call(c["alert_ts"], c["baseline"], c["latest"])
        graded.append({
            **c,
            "outcome": g["outcome"],
            "delta_pct": g["delta_pct"],
        })
    return graded


def summarize(graded: list[dict]) -> dict:
    """Aggregate stat lines. Pure. Unrateable calls are excluded from the
    denominator so the hit rate stays honest."""
    rateable = [c for c in graded if c["outcome"] in ("win", "quiet", "miss")]
    wins = sum(1 for c in rateable if c["outcome"] == "win")
    quiet = sum(1 for c in rateable if c["outcome"] == "quiet")
    misses = sum(1 for c in rateable if c["outcome"] == "miss")
    unrateable = len(graded) - len(rateable)
    hit_rate = (wins / len(rateable) * 100.0) if rateable else None
    return {
        "total": len(graded),
        "rateable": len(rateable),
        "wins": wins,
        "quiet": quiet,
        "misses": misses,
        "unrateable": unrateable,
        "hit_rate_pct": round(hit_rate, 1) if hit_rate is not None else None,
    }


def render_summary(summary: dict) -> str:
    """One-line aggregate. Pure."""
    if summary["total"] == 0:
        return "No alerted games in the lookback window to grade yet."
    rate_line = (
        f"{summary['hit_rate_pct']:.0f}% hit rate"
        if summary["rateable"]
        else "nothing rateable yet"
    )
    detail = (
        f"✅ {summary['wins']} wins · 🔕 {summary['quiet']} quiet · "
        f"📉 {summary['misses']} misses"
    )
    if summary["unrateable"]:
        detail += f" · ({summary['unrateable']} unrateable)"
    return f"{rate_line} — {detail}"


def render_calls(graded: list[dict], limit: int = RENDER_LIMIT) -> list[str]:
    """Per-call lines, newest alert first. Pure."""
    symbols = {"win": "✅", "quiet": "🔕", "miss": "📉", "unrateable": "❔"}
    lines = []
    for c in graded[:limit]:
        outcome_txt = (
            f"{c['delta_pct']:+.0f}% CCU"
            if c["delta_pct"] is not None
            else "no comparable snapshots"
        )
        lines.append(
            f"{symbols[c['outcome']]} **{c['game_name']}** — {outcome_txt}"
        )
    return lines


def scorecard_report(
    db_path: Optional[str] = None,
    lookback_hours: int = LOOKBACK_HOURS,
) -> str:
    """Full scorecard text. Fail-soft like every other read module:
    a data problem shrinks the report, never raises."""
    try:
        graded = evaluate_calls(collect_calls(db_path, lookback_hours))
    except sqlite3.Error as exc:
        logger.warning("Scorecard collection failed: %s", exc)
        graded = []

    lines = [f"📋 **Scorecard — calls from the last {lookback_hours}h**"]
    lines.append("")
    lines.append(render_summary(summarize(graded)))
    call_lines = render_calls(graded)
    if call_lines:
        lines.append("")
        lines.extend(call_lines)
    return "\n".join(lines)
