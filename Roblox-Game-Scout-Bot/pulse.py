"""
Pulse — the bot's own market read, on demand.

`/pulse` (and a section of the morning digest) return what the bot actually
*knows*, compactly and with zero Roblox requests: the games whose CCU is
moving right now (from the bot's own `game_history` snapshots), the games
climbing the Roblox charts cycle-over-cycle (from `chart_positions`), and
the Ascent Radar's recent catches (from `ascent_state`). Everything renders
as plain text a command or a digest can send as-is.

Design constraints, kept by tests:

* NO network. Every selector is a local, indexed read — the pulse is
  instant and cannot make `/status` worse.
* Names resolve from the bot's own records (`alerted_games` first, then
  `watched_games`), the same rule the Ascent Radar uses.
* Every section degrades gracefully: no data means an explicit quiet line,
  never a blank section or a crash.
* The report composer is a pure function of its input lists, so tests
  verify rendering without a database.
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

logger = logging.getLogger("pulse")

# How far back the "recent activity" sections look; matches the digest
# window so /pulse and the morning digest tell one coherent story.
PULSE_WINDOW_HOURS = 24
# Cap per section so the rendered report stays scannable.
PULSE_TOP_LIMIT = 5
# The latest snapshot must be younger than this to count as "moving now".
_FRESH_MINUTES = 30


# ---------------------------------------------------------------------------
# Selectors — each an independent local read; each returns plain dicts.
# ---------------------------------------------------------------------------

def _parse_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _resolve_name(conn, game_id: int) -> Optional[str]:
    """Name from the bot's own records: alerted ledger first, then watched."""
    row = conn.execute(
        "SELECT game_name FROM alerted_games WHERE game_id = ?", (game_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT game_name FROM watched_games "
        "WHERE game_id = ? AND game_name IS NOT NULL "
        "ORDER BY date_added DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    return f"Game {game_id}"


def top_movers(
    db_path: Optional[str] = None,
    limit: int = PULSE_TOP_LIMIT,
    window_hours: int = PULSE_WINDOW_HOURS,
) -> list[dict]:
    """Games with the biggest CCU rise (latest vs first) inside the window.

    A game needs: >= 2 snapshots, a positive baseline (first reading > 0),
    a positive latest reading, and a FRESH latest snapshot (<= 30 min old)
    so a stale spike from hours ago is never presented as current movement.

    Returns dicts {game_id, game_name, first_players, last_players,
    growth_pct, snapshot_count}, growth descending, best `limit` entries.
    """
    db_path = db_path or DATABASE_PATH
    now = datetime.now(timezone.utc)

    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT game_id, players, timestamp
            FROM game_history
            WHERE timestamp >= datetime('now', ?)
            ORDER BY game_id ASC, timestamp ASC
            """,
            (f"-{window_hours} hours",),
        ).fetchall()
    finally:
        conn.close()

    per_game: dict[int, list[tuple[str, int]]] = {}
    for r in rows:
        per_game.setdefault(int(r["game_id"]), []).append(
            ((r["timestamp"] or "")[:19], int(r["players"] or 0))
        )

    results: list[dict] = []
    for gid, snaps in per_game.items():
        if len(snaps) < 2:
            continue
        first_players, last_players = snaps[0][1], snaps[-1][1]
        if first_players <= 0 or last_players <= 0:
            continue
        parsed = _parse_utc(snaps[-1][0])
        if parsed is None or (now - parsed).total_seconds() / 60.0 > _FRESH_MINUTES:
            continue
        growth = (last_players - first_players) / first_players * 100.0
        if growth <= 0:
            continue
        results.append({
            "game_id": gid,
            "first_players": first_players,
            "last_players": last_players,
            "growth_pct": round(growth, 1),
            "snapshot_count": len(snaps),
        })

    results.sort(key=lambda r: r["growth_pct"], reverse=True)
    results = results[:limit]

    # Names resolve in one short pass over the surviving rows only.
    conn = _connect(db_path)
    try:
        for r in results:
            r["game_name"] = _resolve_name(conn, r["game_id"])
    finally:
        conn.close()
    return results


def top_chart_climbers(
    db_path: Optional[str] = None,
    limit: int = PULSE_TOP_LIMIT,
    window_hours: int = PULSE_WINDOW_HOURS,
) -> list[dict]:
    """Games whose chart position improved between the two most recent
    cycles they appeared in (within the window).

    Reads `chart_positions`, groups rows per (game_id, sort_id), and keeps
    a positive delta (prev − latest; positive = climbed) for the game's
    BEST climb across all its sorts. Requires both cycle stamps to be
    non-null so two writes from the same cycle are never treated as a
    baseline/movement pair.

    Returns dicts {game_id, game_name, sort_label, prev_position,
    position, delta}, delta descending, best `limit` entries.
    """
    db_path = db_path or DATABASE_PATH

    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT game_id, sort_id, sort_label, position, cycle_ts
            FROM chart_positions
            WHERE recorded_at >= datetime('now', ?)
            ORDER BY game_id ASC, sort_id ASC, cycle_ts ASC, id ASC
            """,
            (f"-{window_hours} hours",),
        ).fetchall()
    finally:
        conn.close()

    best_by_game: dict[int, dict] = {}
    series: dict[tuple[int, str], list] = {}
    for r in rows:
        game_id = int(r["game_id"])
        if r["cycle_ts"] is None:
            continue  # legacy row without a cycle stamp: uncomparable
        series.setdefault((game_id, r["sort_id"] or ""), []).append(
            (r["cycle_ts"], int(r["position"] or 0), r["sort_label"] or "")
        )

    for (game_id, _sort_id), points in series.items():
        # Group by distinct cycle stamp: a cycle's own rows are duplicates.
        distinct: list[tuple[str, int, str]] = []
        for stamp, pos, label in points:
            if not distinct or stamp != distinct[-1][0]:
                distinct.append((stamp, pos, label))
        if len(distinct) < 2:
            continue  # appeared in only one cycle: no movement yet
        prev_pos = distinct[-2][1]
        last_pos = distinct[-1][1]
        label = distinct[-1][2] or distinct[-2][2]
        delta = prev_pos - last_pos  # positive = climbed
        if delta <= 0:
            continue
        cand = {
            "game_id": game_id,
            "sort_label": label,
            "prev_position": prev_pos,
            "position": last_pos,
            "delta": delta,
        }
        existing = best_by_game.get(game_id)
        if existing is None or cand["delta"] > existing["delta"]:
            best_by_game[game_id] = cand

    results = sorted(best_by_game.values(), key=lambda r: r["delta"], reverse=True)
    results = results[:limit]

    conn = _connect(db_path)
    try:
        for r in results:
            r["game_name"] = _resolve_name(conn, r["game_id"])
    finally:
        conn.close()
    return results


def recent_ascent_events(
    db_path: Optional[str] = None,
    limit: int = PULSE_TOP_LIMIT,
    window_hours: int = PULSE_WINDOW_HOURS,
) -> list[dict]:
    """Radar posts inside the window, newest first.

    Returns dicts {game_id, game_name, kind, ccu, alert_ts} (kind is
    'first' or 'escalate'), at most `limit` entries.
    """
    db_path = db_path or DATABASE_PATH
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT game_id, game_name, last_kind, last_ccu, last_alert_ts
            FROM ascent_state
            WHERE last_alert_ts IS NOT NULL
              AND last_alert_ts >= datetime('now', ?)
            ORDER BY last_alert_ts DESC
            LIMIT ?
            """,
            (f"-{window_hours} hours", limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "game_id": int(r["game_id"]),
            "game_name": r["game_name"] or f"Game {r['game_id']}",
            "kind": r["last_kind"] or "first",
            "ccu": int(r["last_ccu"] or 0),
            "alert_ts": r["last_alert_ts"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Rendering — pure functions over the selector outputs.
# ---------------------------------------------------------------------------

def render_movers(movers: list[dict]) -> list[str]:
    lines = []
    for m in movers:
        lines.append(
            f"📈 **{m['game_name']}** +{m['growth_pct']:.0f}% CCU "
            f"({m['first_players']:,} → {m['last_players']:,}) "
            f"over {m['snapshot_count']} scans"
        )
    return lines


def render_climbers(climbers: list[dict]) -> list[str]:
    lines = []
    for c in climbers:
        sort = c["sort_label"] or "a Roblox chart"
        lines.append(
            f"📊 **{c['game_name']}** climbed #{c['prev_position']} → "
            f"#{c['position']} on {sort}"
        )
    return lines


def render_ascents(events: list[dict]) -> list[str]:
    lines = []
    for e in events:
        tag = "🚀 New ascent" if e["kind"] == "first" else "🔥 Ascent continues"
        lines.append(f"{tag} — **{e['game_name']}** at {e['ccu']:,} CCU")
    return lines


def compose_pulse_report(
    movers: list[dict],
    climbers: list[dict],
    ascents: list[dict],
) -> str:
    """The full /pulse text. Pure — pass the data in, get the text out."""
    parts = [
        f"✳️ **Market pulse — last {PULSE_WINDOW_HOURS}h**",
    ]

    mover_lines = render_movers(movers)
    parts.append("")
    parts.append("**CCU movers (fresh reads):**")
    parts.extend(mover_lines) if mover_lines else parts.append(
        "_No fresh CCU movement in the window yet — snapshots build as scans run._"
    )

    climber_lines = render_climbers(climbers)
    parts.append("")
    parts.append("**Chart climbers (cycle over cycle):**")
    parts.extend(climber_lines) if climber_lines else parts.append(
        "_No chart climbs recorded yet._"
    )

    ascent_lines = render_ascents(ascents)
    parts.append("")
    parts.append("**Ascent Radar (last catches):**")
    parts.extend(ascent_lines) if ascent_lines else parts.append(
        "_Radar quiet — no qualifying breakouts in the window._"
    )

    return "\n".join(parts)


def pulse_report(db_path: Optional[str] = None) -> str:
    """Full report: read local data, compose, return the text.

    Fail-soft: any selector error logs a warning and contributes an empty
    section, so a corrupt table can never crash the command or digest.
    """
    db_path = db_path or DATABASE_PATH
    movers: list[dict] = []
    climbers: list[dict] = []
    ascents: list[dict] = []
    for name, fetch in (
        ("movers", top_movers),
        ("climbers", top_chart_climbers),
        ("ascent events", recent_ascent_events),
    ):
        try:
            if name == "movers":
                movers = fetch(db_path)
            elif name == "climbers":
                climbers = fetch(db_path)
            else:
                ascents = fetch(db_path)
        except sqlite3.Error as exc:
            logger.warning("Pulse %s query failed: %s", name, exc)
    return compose_pulse_report(movers, climbers, ascents)
