"""
Native growth history.

Roblox publishes no historical CCU/visit series for an experience, so
growth-over-time can only come from data the bot records itself. Every
scanned game is written to `game_history` by `scanner.save_game_snapshot()`
and retained for `database.GAME_HISTORY_RETENTION_DAYS` (30) — deeper than
the 7 days the previous third-party integration (Atlas) could offer, with no
external dependency and no browser-spoofed requests.

This module replaces `atlas_api.py`. It keeps the same caller contract: a
summary returns `{}` (never `0%`) when there is not enough history, so
callers keep treating absence as "unknown", not "flat".
"""

import logging
import sqlite3
from typing import Optional

from config import DATABASE_PATH, GROWTH_WINDOW_DAYS

logger = logging.getLogger("history")


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract
    (busy_timeout 30 s, synchronous=NORMAL, temp_store=MEMORY)."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn

# Embed-facing window in days. The store keeps the full 30 days regardless.
DEFAULT_WINDOW_DAYS = GROWTH_WINDOW_DAYS


def get_window_snapshots(game_id: int, days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Return snapshots for a game inside the window, oldest first.

    Each row is {"players": int, "visits": int, "timestamp": str}. Returns an
    empty list when the game has never been scanned or the id is unusable.
    """
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return []

    conn = _connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT players, visits, timestamp
        FROM game_history
        WHERE game_id = ?
          AND timestamp >= datetime('now', ?)
        ORDER BY timestamp ASC
        """,
        (game_id, f"-{int(days)} days"),
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {"players": row[0], "visits": row[1], "timestamp": row[2]}
        for row in rows
    ]


def compute_growth_pct(first, last) -> Optional[float]:
    """Percentage change between two readings.

    Returns None — not 0.0 — when either value is missing or the baseline is
    zero, because "we cannot tell" and "it did not move" are different facts
    and callers render them differently.
    """
    try:
        first_value = float(first)
        last_value = float(last)
    except (TypeError, ValueError):
        return None
    if first_value <= 0:
        return None
    return ((last_value - first_value) / first_value) * 100.0


def growth_summary(game_id: int, days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Growth over the window, computed from the bot's own snapshots.

    Returns {} when fewer than two snapshots exist in the window — the caller
    should skip the growth fields entirely rather than print 0%.

    Keys when history exists:
        ccu_growth_pct    float | None  (None if the baseline CCU was 0)
        visits_growth_pct float | None  (None if the baseline visits was 0)
        history_days      int           window actually used
        snapshot_count    int           snapshots in that window
    """
    try:
        gid = int(game_id)  # None / junk -> ValueError, same contract as before
    except (TypeError, ValueError):
        return {}
    return growth_summaries_bulk([gid], days=days).get(gid, {})


def growth_summaries_bulk(
    game_ids, days: int = DEFAULT_WINDOW_DAYS
) -> dict[int, dict]:
    """growth_summary() for many games in ONE indexed query + one connection.

    A scan cycle's survivors each used to pay their own connection + query;
    this fetches every candidate's window snapshots in a single indexed read
    and computes the same per-game summaries the single-game path does.
    Returns {game_id: summary-dict}; games with <2 snapshots in the window
    are absent from the dict (same contract as growth_summary returning
    {}). Days=0 or a None/empty input returns {} cheaply.
    """
    ids: list[int] = []
    for gid in game_ids or []:
        try:
            ids.append(int(gid))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(ids))  # dedupe, keep order
    if not ids or days <= 0:
        return {}

    placeholders = ",".join("?" for _ in ids)
    conn = _connect(DATABASE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT game_id, players, visits, timestamp
            FROM game_history
            WHERE game_id IN ({placeholders})
              AND timestamp >= datetime('now', ?)
            ORDER BY game_id, timestamp ASC
            """,
            (*ids, f"-{int(days)} days"),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    grouped: dict[int, list[tuple]] = {}
    for gid, players, visits, timestamp in rows:
        grouped.setdefault(int(gid), []).append((players, visits, timestamp))

    summaries: dict[int, dict] = {}
    for gid, points in grouped.items():
        if len(points) < 2:
            continue
        first_players, first_visits = points[0][0], points[0][1]
        last_players, last_visits = points[-1][0], points[-1][1]
        summaries[gid] = {
            "ccu_growth_pct": compute_growth_pct(first_players, last_players),
            "visits_growth_pct": compute_growth_pct(first_visits, last_visits),
            "history_days": int(days),
            "snapshot_count": len(points),
        }
    return summaries
    points = get_window_snapshots(game_id, days=days)
    if len(points) < 2:
        return {}

    first, last = points[0], points[-1]
    return {
        "ccu_growth_pct": compute_growth_pct(first.get("players"), last.get("players")),
        "visits_growth_pct": compute_growth_pct(first.get("visits"), last.get("visits")),
        "history_days": int(days),
        "snapshot_count": len(points),
    }


def count_snapshots() -> int:
    """Total rows in game_history — used by the startup health check."""
    try:
        conn = _connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM game_history")
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug("Snapshot count failed: %s", exc)
        return 0
