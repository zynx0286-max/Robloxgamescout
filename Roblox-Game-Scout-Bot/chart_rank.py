"""
Chart-rank signal.

Roblox Charts is not just a discovery source — *where* a game sits on a
chart, and whether it is climbing, is one of the strongest early signals an
acquisition scout gets. A game at #4 on Up-and-Coming is a different
proposition from one at #90, even though both carry the same `on_charts`
flag.

This module records every game's position within its chart sort once per
scan cycle (written by `data_collector` right after discovery), and answers
three questions for anything later in the pipeline:

  * `chart_rank`      — this cycle's position (1 = top of the chart)
  * `chart_rank_delta`— movement vs the previous cycle (positive = climbed)
  * `chart_best`      — best position ever recorded (lower is better)

Cycle separation: rows written by one `record_positions()` call all share
that call's cycle timestamp, and the queries for deltas simply ignore rows
at or after it — so "same cycle" rows can never be mistaken for a baseline.
Retention matches `game_history` (30 days), pruned opportunistically.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import DATABASE_PATH
from database import _connect

logger = logging.getLogger("chart_rank")

# chart_positions grows by one row per charted game per cycle (~124/cycle
# with the default sorts). 30 days is ample for delta and best-rank math.
CHART_POSITIONS_RETENTION_DAYS = 30
CHART_POSITIONS_PRUNE_INTERVAL_HOURS = 6
_last_chart_prune: Optional[datetime] = None

# Timestamp shared by every row written during the current cycle. Written by
# record_positions() and consulted by the previous-* queries so rows from the
# cycle being recorded are never treated as their own baseline.
_current_cycle_ts: Optional[str] = None


def record_positions(positions: list[tuple[int, str, str, int]]) -> int:
    """Record this cycle's chart positions in bulk.

    positions: list of (game_id, sort_id, sort_label, position), position
    1-based (1 = top of the chart). Returns the number of rows written.

    Every row of one call shares that call's cycle timestamp (stored on the
    row at microsecond precision), which is what lets the previous-* queries
    tell cycles apart even when two cycles run within the same wall-clock
    second. Also runs the opportunistic prune.
    """
    global _last_chart_prune, _current_cycle_ts

    clean = []
    for game_id, sort_id, sort_label, position in positions or []:
        try:
            clean.append(
                (int(game_id), str(sort_id or ""), str(sort_label or ""), int(position))
            )
        except (TypeError, ValueError):
            continue
    if not clean:
        return 0

    # Microsecond precision: two record calls in the same second must still
    # be distinguishable cycles, and the stored string must compare against
    # itself lexically in chronological order.
    _current_cycle_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    conn = _connect()
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO chart_positions (game_id, sort_id, sort_label, position, cycle_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        [row + (_current_cycle_ts,) for row in clean],
    )
    written = cursor.rowcount
    conn.commit()
    conn.close()

    _maybe_prune()
    return written or len(clean)


def _maybe_prune() -> None:
    """Drop rows older than the retention window, at most every few hours."""
    global _last_chart_prune

    now = datetime.now(timezone.utc)
    if (
        _last_chart_prune is not None
        and now - _last_chart_prune
        < timedelta(hours=CHART_POSITIONS_PRUNE_INTERVAL_HOURS)
    ):
        return
    try:
        cutoff = (
            now.replace(tzinfo=None) - timedelta(days=CHART_POSITIONS_RETENTION_DAYS)
        ).isoformat(sep=" ")
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chart_positions WHERE recorded_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        _last_chart_prune = now
        if deleted:
            logger.info(
                "Pruned %d chart_positions rows older than %dd",
                deleted,
                CHART_POSITIONS_RETENTION_DAYS,
            )
    except sqlite3.Error as exc:
        logger.warning("chart_positions pruning failed: %s", exc)


def previous_position(game_id: int, sort_id: str) -> Optional[int]:
    """Position from the most recent *previous* cycle for one sort.

    Rows written during the current cycle are excluded — they share its
    cycle_ts, and the comparison is strictly less-than. None when the game
    was not on this sort in any earlier retained cycle.
    """
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return None
    before = _current_cycle_ts or "9999-12-31 23:59:59.999999"
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT position FROM chart_positions
        WHERE game_id = ?
          AND sort_id = ?
          AND (cycle_ts IS NULL OR cycle_ts < ?)
        ORDER BY cycle_ts DESC, id DESC
        LIMIT 1
        """,
        (game_id, str(sort_id or ""), before),
    )
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None


def best_position(game_id: int) -> Optional[int]:
    """Best (lowest = highest on a chart) position ever recorded. None if never charted."""
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return None
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MIN(position) FROM chart_positions WHERE game_id = ?",
        (game_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row and row[0] is not None:
        return int(row[0])
    return None


def attach_rank_signals_bulk(games: list[dict]) -> int:
    """attach_rank_signal() for a whole scan cycle in indexed bulk reads.

    Each charted game costs two queries (previous position + best ever);
    a 260-game cycle with ~124 charted games pays them one connection at
    a time today. This prefetches, in TWO queries over the whole set:
      * every game's best rank within the cycle's sorts, and
      * every game's best-ever rank,
    then runs the identical per-game resolution logic in-process.

    Returns the number of games whose signal was attached. The per-game
    resolution logic is identical to attach_rank_signal(); callers can use
    either form.
    """
    ids: list[int] = []
    for game in games or []:
        gid = game.get("id")
        try:
            ids.append(int(gid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0

    # Prefetch (a) best prior positions per (game, sort) before this cycle,
    # and (b) best-ever positions per game, in one query each.
    placeholders = ",".join("?" for _ in ids)
    before = _current_cycle_ts or "9999-12-31 23:59:59.999999"
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT game_id, sort_id, position
            FROM chart_positions
            WHERE game_id IN ({placeholders})
              AND (cycle_ts IS NULL OR cycle_ts < ?)
            """,
            (*ids, before),
        )
        prior_rows = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT game_id, MIN(position)
            FROM chart_positions
            WHERE game_id IN ({placeholders})
            GROUP BY game_id
            """,
            (*ids,),
        )
        best_rows = dict(cursor.fetchall())
    finally:
        conn.close()

    # (game_id, sort_id) -> best prior position for that sort.
    prior_best: dict[tuple, int] = {}
    for gid, sort_id, position in prior_rows:
        key = (int(gid), str(sort_id or ""))
        cur = prior_best.get(key)
        if cur is None or int(position) < cur:
            prior_best[key] = int(position)

    attached = 0
    for game in games or []:
        appearances = [
            (sort_id, label, rank)
            for sort_id, label, rank in (game.get("chart_appearances") or [])
            if rank
        ]
        if not appearances and game.get("chart_rank"):
            appearances = [
                (game.get("charts_sort_id") or "", game.get("charts_sort") or "",
                 game.get("chart_rank"))
            ]
        if not appearances:
            continue
        sort_id, label, rank = min(appearances, key=lambda item: item[2])
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue

        game["chart_rank"] = rank
        if label:
            game["charts_sort"] = label

        gid = game.get("id")
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            gid_int = None
        if gid_int is not None:
            prev = prior_best.get((gid_int, str(sort_id or "")))
            if prev is not None:
                game["chart_rank_delta"] = prev - rank
            best = best_rows.get(gid_int)
            if best is not None:
                game["chart_best"] = min(int(best), rank)
        attached += 1

    return attached


def attach_rank_signal(game: dict) -> None:
    """Attach the chart-rank fields one game dict needs for scoring/embeds.

    Works from the game's `chart_appearances` list (all sorts it was seen

    Works from the game's `chart_appearances` list (all sorts it was seen
    under this cycle) or, failing that, its single `chart_rank` stub field.
    Sets, for charted games:

        chart_rank        int    best position across this cycle's sorts
        charts_sort       str    label of the sort that position came from
        chart_rank_delta  int    prev - current (positive = climbed), or None
                                 when there is no earlier cycle to compare
        chart_best        int    best position ever recorded

    Uncharted games are left untouched.
    """
    appearances = [
        (sort_id, label, rank)
        for sort_id, label, rank in (game.get("chart_appearances") or [])
        if rank
    ]
    if not appearances and game.get("chart_rank"):
        appearances = [
            (game.get("charts_sort_id") or "", game.get("charts_sort") or "",
             game.get("chart_rank"))
        ]
    if not appearances:
        return

    # The best appearance is the one worth reporting: a game at #3 on
    # Top Trending and #80 on Top Playing Now is a #3 game.
    sort_id, label, rank = min(appearances, key=lambda item: item[2])
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return

    game["chart_rank"] = rank
    if label:
        game["charts_sort"] = label

    prev = previous_position(game.get("id"), sort_id)
    if prev is not None:
        game["chart_rank_delta"] = prev - rank  # positive = climbed

    best = best_position(game.get("id"))
    if best is not None:
        game["chart_best"] = min(best, rank)


def chart_score(game: dict) -> int:
    """0-10 points for the Scout Score breakdown.

    Near the top of a chart is where breakout happens, so most of the
    weight sits in the first few positions. A fast climb earns a small
    bonus regardless of absolute position — a game storming up from #60 is
    a live signal even if it is not chart-topping yet.
    """
    rank = game.get("chart_rank")
    if not rank:
        return 0
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return 0

    if rank <= 3:
        pts = 10
    elif rank <= 10:
        pts = 8
    elif rank <= 25:
        pts = 6
    elif rank <= 50:
        pts = 4
    else:
        pts = 2

    try:
        delta = game.get("chart_rank_delta")
        if delta is not None and int(delta) >= 5:
            pts = min(10, pts + 2)
    except (TypeError, ValueError):
        pass

    return pts


def chart_score_text(game: dict) -> str:
    """Human-readable reason line for the Scout Score breakdown."""
    rank = game.get("chart_rank")
    if not rank:
        return "📊 Not on a chart this cycle"
    label = game.get("charts_sort") or "Roblox chart"
    bits = [f"#{rank} on {label}"]
    delta = game.get("chart_rank_delta")
    if isinstance(delta, int):
        if delta > 0:
            bits.append(f"▲{delta} vs last cycle")
        elif delta < 0:
            bits.append(f"▼{-delta} vs last cycle")
        else:
            bits.append("holding position")
    best = game.get("chart_best")
    if isinstance(best, int) and best < rank:
        bits.append(f"best #{best}")
    return "📊 " + ", ".join(bits)


def latest_positions(game_id: int) -> list[tuple[str, int]]:
    """Most recent position per chart sort for one game, best first.

    Used by the tracker embed. Empty when the game has never been charted
    (or the id is unusable).
    """
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return []
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT sort_label, position FROM chart_positions
        WHERE game_id = ?
          AND id IN (
              SELECT MAX(id) FROM chart_positions
              WHERE game_id = ?
              GROUP BY sort_id
          )
        ORDER BY position ASC
        """,
        (game_id, game_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return [(row[0] or "?", int(row[1])) for row in rows]
