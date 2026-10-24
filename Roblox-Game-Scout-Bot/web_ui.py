"""The Scout web console — server-rendered, zero-dependency pages.

Six views, all reading the bot's own SQLite stores (never Roblox):

    /dashboard   market overview: opportunity board, CCU movers, chart
                 climbers, radar catches, latest alerts and portfolio
    /market      market weather: breadth, move distribution, leaders,
                 drawdowns and per-chart footprint
    /games       searchable / filterable index of every game the bot has
                 alerted, watched, or put on the portfolio, with CSV export
    /compare     any two games side by side: metric table plus both charts
    /watchlist   tracking desk: every watched game, freshness and momentum
    /scorecard   every alert graded at a fixed 72h horizon
    /game/{id}   one game's full local dossier: a deterministic Scout read,
                 CCU history with a Scout-market benchmark, alert record,
                 watch state, chart positions, radar state, AI verdict and a
                 dated signal timeline

Design contract (same as the old pages): no external assets, no network,
every interpolated value escaped, exact empty states instead of mock data.
The rendering helpers are pure string builders; data access is read-only
and degrades to an empty state if a table is missing.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import portfolio_db

logger = logging.getLogger(__name__)
UTC = timezone.utc

_RANGE_HOURS = (("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30))
_DEFAULT_RANGE = "7d"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _esc(value) -> str:
    """Escape any value for HTML text or a quoted attribute."""
    return html.escape("" if value is None else str(value), quote=True)


def _now() -> datetime:
    """Naive UTC now, the format SQLite's CURRENT_TIMESTAMP uses."""
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_ts(value) -> Optional[datetime]:
    """Parse SQLite timestamps into naive UTC, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("T", " ")
        if not text:
            return None
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _time_ago(value) -> str:
    dt = _parse_ts(value)
    if dt is None:
        return "unknown"
    seconds = (_now() - dt).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _fmt_ts(value) -> str:
    dt = _parse_ts(value)
    return dt.strftime("%b %d, %H:%M UTC") if dt else "—"


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_compact(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}b"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.1f}m"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:,.0f}"


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pct_change(first, last) -> Optional[float]:
    try:
        first_f = float(first)
        last_f = float(last)
    except (TypeError, ValueError):
        return None
    if first_f <= 0:
        return None
    return round((last_f - first_f) / first_f * 100.0, 1)


def _pct_html(value) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    if value > 0:
        return f'<span class="up">+{value:.1f}%</span>'
    if value < 0:
        return f'<span class="down">{value:.1f}%</span>'
    return '<span class="flat">0.0%</span>'


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _connect():
    return portfolio_db._connect()


def _try_rows(sql: str, params=()):
    """Return a list of dict rows, or None when the query cannot run."""
    conn = None
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        logger.debug("dashboard query failed: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def _rows(sql: str, params=()):
    return _try_rows(sql, params) or []


def _series_by_game(game_ids, hours: int = 24 * 30) -> dict[int, list[dict]]:
    """CCU history for many games in one (chunked) indexed read."""
    ids = []
    for game_id in game_ids:
        try:
            ids.append(int(game_id))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(ids))
    groups: dict[int, list[dict]] = {gid: [] for gid in ids}
    for chunk in _chunks(ids, 400):
        placeholders = ",".join("?" for _ in chunk)
        rows = _rows(
            f"""
            SELECT game_id, players, visits, timestamp
            FROM game_history
            WHERE game_id IN ({placeholders})
              AND timestamp >= datetime('now', ?)
            ORDER BY game_id ASC, timestamp ASC, id ASC
            """,
            (*chunk, f"-{int(hours)} hours"),
        )
        for row in rows:
            try:
                gid = int(row["game_id"])
            except (TypeError, ValueError):
                continue
            groups.setdefault(gid, []).append({
                "players": row["players"],
                "visits": row["visits"],
                "timestamp": row["timestamp"],
                "dt": _parse_ts(row["timestamp"]),
            })
    return groups


def _window(points: list[dict], hours: int) -> list[dict]:
    cutoff = _now() - timedelta(hours=hours)
    return [
        p for p in points
        if p.get("dt") is not None and p["dt"] >= cutoff
    ]


# ---------------------------------------------------------------------------
# Tracked-market benchmark
# ---------------------------------------------------------------------------

# The benchmark is only meaningful when several games contribute to a bucket.
_BENCH_MIN_GAMES = 3
_BENCH_TTL_SECONDS = 300
_BENCH_CACHE = {"key": None, "at": 0.0, "series": {}}


def _history_max_id() -> int:
    rows = _try_rows("SELECT MAX(id) AS latest FROM game_history")
    if not rows:
        return 0
    value = rows[0].get("latest")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _benchmark_raw() -> dict[int, list[tuple]]:
    """Per-game 30-day series for the games the console calls tracked.

    Cached for five minutes and keyed by the database plus its newest
    history row, so a scan invalidates it without a manual flush. The
    universe is alerts + watches + portfolio projects — the games Scout
    has actually identified — never the whole scanned catalog.
    """
    key = (str(portfolio_db.DATABASE), _history_max_id())
    now = time.monotonic()
    if (
        _BENCH_CACHE["key"] == key
        and now - float(_BENCH_CACHE["at"] or 0) < _BENCH_TTL_SECONDS
    ):
        return _BENCH_CACHE["series"]

    ids = set()
    for rows in (_alerted_rows(), _watched_rows(), _portfolio_rows()):
        for row in rows:
            gid = _as_int(row.get("game_id"))
            if gid is not None:
                ids.add(gid)

    series: dict[int, list[tuple]] = {}
    if len(ids) >= _BENCH_MIN_GAMES:
        for gid, points in _series_by_game(sorted(ids), hours=24 * 30).items():
            clean = [
                (p["dt"], float(p["players"]))
                for p in points
                if p.get("dt") is not None
                and isinstance(p.get("players"), (int, float))
            ]
            if len(clean) >= 2:
                series[gid] = clean

    _BENCH_CACHE.update({"key": key, "at": now, "series": series})
    return series


def _bench_bucket(dt: datetime, hours: int) -> datetime:
    """Coarser buckets for longer ranges; 30m / 2h / 6h."""
    if hours <= 24:
        minute = 30 if dt.minute >= 30 else 0
        return dt.replace(minute=minute, second=0, microsecond=0)
    if hours <= 24 * 7:
        return dt.replace(hour=dt.hour - dt.hour % 2, minute=0,
                          second=0, microsecond=0)
    return dt.replace(hour=dt.hour - dt.hour % 6, minute=0,
                      second=0, microsecond=0)


def _benchmark_range(raw: dict, hours: int, base_value) -> dict:
    """The Scout market's median path, scaled to a game's start value.

    Each game is normalised to its first reading inside the window, so the
    index measures the median *move* rather than being dominated by one
    huge game. Buckets need at least three contributing games; without
    that coverage the function returns an empty result, and the page says
    nothing rather than drawing a line from two games.
    """
    empty = {"points": [], "growth": None, "games": 0}
    if not raw or not isinstance(base_value, (int, float)) or base_value <= 0:
        return empty

    cutoff = _now() - timedelta(hours=hours)
    buckets: dict[datetime, list[float]] = {}
    games = 0
    for points in raw.values():
        window = [(dt, value) for dt, value in points if dt >= cutoff]
        if len(window) < 2:
            continue
        first = window[0][1]
        if first <= 0:
            continue
        games += 1
        for dt, value in window:
            bucket = _bench_bucket(dt, hours)
            buckets.setdefault(bucket, []).append(value / first * 100.0)

    if games < _BENCH_MIN_GAMES:
        return empty

    path = []
    for bucket in sorted(buckets):
        values = buckets[bucket]
        if len(values) < _BENCH_MIN_GAMES:
            continue
        path.append((
            bucket,
            float(base_value) * statistics.median(values) / 100.0,
        ))

    if len(path) < 2:
        return empty
    growth = (path[-1][1] - float(base_value)) / float(base_value) * 100.0
    return {"points": path, "growth": round(growth, 1), "games": games}


# ---------------------------------------------------------------------------
# Data reads
# ---------------------------------------------------------------------------

def _overview_data() -> dict:
    conn = None
    data = {
        "tracked": 0, "snapshots": 0, "alerts": 0, "alerts_7d": 0,
        "watch_games": 0, "human_watches": 0, "auto_watches": 0,
        "charted": 0, "radar_24h": 0, "scans_24h": 0,
        "last_scan": None, "last_snapshot": None,
    }
    try:
        conn = _connect()
        cursor = conn.cursor()

        def one(key, sql, default=0):
            try:
                row = cursor.execute(sql).fetchone()
            except sqlite3.Error:
                data[key] = default
                return
            data[key] = (row[0] if row and row[0] is not None else default)

        one("tracked", "SELECT COUNT(DISTINCT game_id) FROM game_history")
        one("snapshots", "SELECT COUNT(*) FROM game_history")
        one("alerts", "SELECT COUNT(*) FROM alerted_games")
        one("alerts_7d", "SELECT COUNT(*) FROM alerted_games "
                         "WHERE first_alerted >= datetime('now','-7 days')")
        one("watch_games", "SELECT COUNT(DISTINCT game_id) FROM watched_games")
        one("human_watches", "SELECT COUNT(DISTINCT game_id) FROM watched_games "
                             "WHERE source != 'auto'")
        one("auto_watches", "SELECT COUNT(*) FROM watched_games WHERE source = 'auto'")
        one("charted", "SELECT COUNT(DISTINCT game_id) FROM chart_positions "
                       "WHERE recorded_at >= datetime('now','-30 days')")
        one("radar_24h", "SELECT COUNT(*) FROM ascent_state "
                         "WHERE last_alert_ts >= datetime('now','-1 day')")
        one("scans_24h", "SELECT COUNT(*) FROM scan_log WHERE event = 'scan_complete' "
                         "AND timestamp >= datetime('now','-1 day')")
        one("last_scan", "SELECT MAX(timestamp) FROM scan_log "
                         "WHERE event = 'scan_complete'", None)
        one("last_snapshot", "SELECT MAX(timestamp) FROM game_history", None)
    finally:
        if conn is not None:
            conn.close()
    return data


_ALERT_SQL = (
    "SELECT game_id, game_name, best_scout_score, alert_count, first_alerted "
    "FROM alerted_games"
)
_ALERT_SQL_LEGACY = (
    "SELECT game_id, game_name, NULL AS best_scout_score, 1 AS alert_count, "
    "first_alerted FROM alerted_games"
)


def _alerted_rows() -> list[dict]:
    """Alerted-game rows, tolerating a legacy table without score columns."""
    rows = _try_rows(_ALERT_SQL)
    if rows is None:
        rows = _try_rows(_ALERT_SQL_LEGACY)
    return rows or []


def _watched_rows() -> list[dict]:
    return _rows(
        """
        SELECT game_id,
               COALESCE(MAX(NULLIF(game_name, '')), '') AS game_name,
               COUNT(*) AS watch_count,
               SUM(CASE WHEN source = 'auto' THEN 1 ELSE 0 END) AS auto_count,
               MAX(last_players) AS last_players,
               MAX(last_visits) AS last_visits,
               MAX(last_checked) AS last_checked,
               MIN(date_added) AS first_watched
        FROM watched_games
        GROUP BY game_id
        """
    )


def _portfolio_rows() -> list[dict]:
    return _rows(
        """
        SELECT game_id, display_name, role, description, project_url,
               roblox_url, discord_url, thumbnail_url, created_at
        FROM portfolio_games
        WHERE visible = 1
        ORDER BY sort_order ASC, display_name ASC
        """
    )


def _chart_best_map() -> dict[int, int]:
    rows = _rows(
        """
        SELECT game_id, MIN(position) AS best
        FROM chart_positions
        WHERE recorded_at >= datetime('now','-30 days')
        GROUP BY game_id
        """
    )
    best = {}
    for row in rows:
        try:
            best[int(row["game_id"])] = int(row["best"])
        except (TypeError, ValueError):
            continue
    return best


def _ai_map() -> dict[int, dict]:
    rows = _rows(
        """
        SELECT game_id, verdict, confidence, strengths, risks,
               recommendation, last_checked
        FROM ai_analysis_cache
        """
    )
    return {int(r["game_id"]): r for r in rows if r.get("game_id") is not None}


def _radar_map() -> dict[int, dict]:
    rows = _rows(
        """
        SELECT game_id, game_name, last_kind, last_ccu, first_seen_ts,
               last_alert_ts
        FROM ascent_state
        """
    )
    return {int(r["game_id"]): r for r in rows if r.get("game_id") is not None}


def _market_data() -> dict:
    """Market pulse selectors plus the rules they already enforce."""
    import pulse

    db_path = portfolio_db.DATABASE

    def safe(fn, **kwargs):
        try:
            return fn(db_path=db_path, **kwargs)
        except Exception as exc:  # a quiet board must not take the page down
            logger.debug("market selector failed: %s", exc)
            return []

    movers = safe(pulse.top_movers, limit=6)
    climbers = safe(pulse.top_chart_climbers, limit=6)
    ascents = safe(pulse.recent_ascent_events, limit=6)
    series = _series_by_game([m["game_id"] for m in movers], hours=24)
    for mover in movers:
        mover["spark"] = [
            p["players"] for p in series.get(int(mover["game_id"]), [])
            if p.get("players") is not None
        ]
    alerts = sorted(
        _alerted_rows(),
        key=lambda r: str(r.get("first_alerted") or ""),
        reverse=True,
    )[:6]
    return {
        "movers": movers,
        "climbers": climbers,
        "ascents": ascents,
        "recent_alerts": alerts,
    }


def _game_index() -> dict:
    """Every identified game, with one bulk history read for all metrics."""
    alerted = {int(r["game_id"]): r for r in _alerted_rows() if r.get("game_id") is not None}
    watched = {int(r["game_id"]): r for r in _watched_rows() if r.get("game_id") is not None}
    portfolio = {int(r["game_id"]): r for r in _portfolio_rows() if r.get("game_id") is not None}
    chart_best = _chart_best_map()
    ai = _ai_map()
    radar = _radar_map()

    ids = sorted(set(alerted) | set(watched) | set(portfolio))
    series = _series_by_game(ids, hours=24 * 30)

    rows = []
    for gid in ids:
        alert = alerted.get(gid) or {}
        watch = watched.get(gid) or {}
        project = portfolio.get(gid) or {}
        points = series.get(gid, [])
        week = _window(points, 24 * 7)
        latest = points[-1] if points else None

        name = (
            (alert.get("game_name") or "").strip()
            or (watch.get("game_name") or "").strip()
            or (project.get("display_name") or "").strip()
            or f"Game {gid}"
        )
        players = latest["players"] if latest else watch.get("last_players")
        visits = latest["visits"] if latest else watch.get("last_visits")
        last_seen = (
            latest["timestamp"] if latest
            else watch.get("last_checked")
        )
        growth = None
        if len(week) >= 2:
            growth = _pct_change(week[0]["players"], week[-1]["players"])
        spark = [
            p["players"] for p in week if p.get("players") is not None
        ]
        score = alert.get("best_scout_score")
        flags = []
        if alert:
            flags.append("alerted")
        if watch:
            flags.append("watched")
        if project:
            flags.append("portfolio")
        if gid in chart_best:
            flags.append("charts")
        if ai.get(gid):
            flags.append("ai")
        if growth is not None and growth >= 10:
            flags.append("rising")
        if growth is not None and growth <= -10:
            flags.append("falling")

        rows.append({
            "game_id": gid,
            "name": name,
            "score": score,
            "alerts": int(alert.get("alert_count") or 0),
            "first_alerted": alert.get("first_alerted"),
            "watch_count": int(watch.get("watch_count") or 0),
            "auto_count": int(watch.get("auto_count") or 0),
            "portfolio": bool(project),
            "role": project.get("role") or "",
            "thumbnail": project.get("thumbnail_url") or "",
            "chart_best": chart_best.get(gid),
            "ai": ai.get(gid),
            "radar": radar.get(gid),
            "players": players,
            "visits": visits,
            "last_seen": last_seen,
            "growth": growth,
            "spark": spark,
            "flags": flags,
        })

    rows.sort(key=lambda r: (
        -(r["score"] if isinstance(r["score"], (int, float)) else -1),
        -(r["players"] if isinstance(r["players"], (int, float)) else -1),
        r["name"].casefold(),
    ))
    return {"rows": rows, "total": len(rows)}


_WATCH_STALE_HOURS = 6


def _watchlist_data() -> dict:
    """The tracking desk: watched games, freshness and momentum, no user ids."""
    raw = _rows(
        """
        SELECT game_id, game_name, last_players, last_visits, last_checked,
               date_added, source
        FROM watched_games
        """
    )
    grouped: dict[int, dict] = {}
    for row in raw:
        gid = _as_int(row.get("game_id"))
        if gid is None:
            continue
        entry = grouped.setdefault(gid, {
            "game_id": gid,
            "name": "",
            "watch_count": 0,
            "human": 0,
            "auto": 0,
            "last_players": None,
            "last_visits": None,
            "last_checked": None,
            "date_added": None,
        })
        name = (row.get("game_name") or "").strip()
        if name and not entry["name"]:
            entry["name"] = name
        entry["watch_count"] += 1
        if (row.get("source") or "user") == "auto":
            entry["auto"] += 1
        else:
            entry["human"] += 1
        for key in ("last_players", "last_visits"):
            value = row.get(key)
            if isinstance(value, (int, float)):
                entry[key] = value if entry[key] is None else max(entry[key], value)
        for key, mode in (("last_checked", "max"), ("date_added", "min")):
            value = row.get(key)
            if not value:
                continue
            current = entry[key]
            if current is None:
                entry[key] = value
            elif mode == "max":
                entry[key] = max(str(current), str(value))
            else:
                entry[key] = min(str(current), str(value))

    ids = list(grouped)
    series = _series_by_game(ids, hours=24 * 30)
    alerts = {int(r["game_id"]): r for r in _alerted_rows()
              if r.get("game_id") is not None}
    chart_best = _chart_best_map()
    ai = _ai_map()
    now = _now()

    rows = []
    for gid, entry in grouped.items():
        points = [
            p for p in series.get(gid, [])
            if p.get("players") is not None and p.get("dt") is not None
        ]
        latest = points[-1] if points else None
        day = _window(points, 24)
        week = _window(points, 24 * 7)
        two_weeks = _window(points, 24 * 14)
        growth_24h = (
            _pct_change(day[0]["players"], day[-1]["players"])
            if len(day) >= 2 else None
        )
        growth_7d = (
            _pct_change(week[0]["players"], week[-1]["players"])
            if len(week) >= 2 else None
        )
        players = latest["players"] if latest else entry["last_players"]
        visits = latest["visits"] if latest else entry["last_visits"]
        spark = [p["players"] for p in two_weeks]

        since_growth = None
        added = _parse_ts(entry["date_added"])
        if added is not None:
            after = [p for p in points if p["dt"] >= added]
            if after and after[-1].get("players"):
                since_growth = _pct_change(after[0]["players"], after[-1]["players"])

        checked_dt = _parse_ts(entry["last_checked"])
        stale = (
            checked_dt is None
            or (now - checked_dt).total_seconds() > _WATCH_STALE_HOURS * 3600
        )
        if stale:
            status = "Stale"
        elif growth_24h is not None and growth_24h >= 20:
            status = "Spiking"
        elif ((growth_24h is not None and growth_24h >= 8)
              or (growth_7d is not None and growth_7d >= 10)):
            status = "Rising"
        elif ((growth_7d is not None and growth_7d <= -10)
              or (growth_24h is not None and growth_24h <= -15)):
            status = "Cooling"
        else:
            status = "Quiet"

        alert = alerts.get(gid) or {}
        name = entry["name"] or alert.get("game_name") or f"Game {gid}"
        momentum = max(
            [v for v in (growth_24h, growth_7d) if v is not None],
            default=None,
        )
        flags = ["watched", status.lower(), "stale" if stale else ""]
        if entry["human"]:
            flags.append("mine")
        if entry["auto"]:
            flags.append("auto")
        if status in ("Spiking", "Rising"):
            flags.append("moving")

        rows.append({
            "game_id": gid,
            "name": name,
            "players": players,
            "visits": visits,
            "growth_24h": growth_24h,
            "growth_7d": growth_7d,
            "since_watch": since_growth,
            "spark": spark,
            "watch_count": entry["watch_count"],
            "human": entry["human"],
            "auto": entry["auto"],
            "last_checked": entry["last_checked"],
            "status": status,
            "alert_score": alert.get("best_scout_score"),
            "chart_best": chart_best.get(gid),
            "ai": ai.get(gid),
            "momentum": momentum,
            "flags": [flag for flag in flags if flag],
        })

    rows.sort(key=lambda row: (
        -(row["momentum"] if row["momentum"] is not None else -1e9),
        -(row["players"] if isinstance(row["players"], (int, float)) else -1),
        row["name"].casefold(),
    ))
    strongest = next(
        (row for row in rows if row["growth_24h"] is not None), None
    )
    return {
        "rows": rows,
        "human": sum(1 for row in rows if row["human"]),
        "auto": sum(1 for row in rows if row["auto"]),
        "stale": sum(1 for row in rows if row["status"] == "Stale"),
        "moving": sum(1 for row in rows if row["status"] in ("Spiking", "Rising")),
        "strongest": strongest,
    }


# ---------------------------------------------------------------------------
# Track record — every alert graded at a fixed 72h horizon
# ---------------------------------------------------------------------------

_RECORD_WINDOW_DAYS = 30
_RECORD_HORIZON_HOURS = 72
_RECORD_GRACE_HOURS = 24
_RECORD_HIT_PCT = 20.0
_RECORD_MISS_PCT = -20.0


def _grade_record_call(baseline, outcome) -> dict:
    """Grade one alert at the fixed horizon. Pure and explicit."""
    try:
        base = float(baseline)
        final = float(outcome)
    except (TypeError, ValueError):
        return {"status": "unrateable", "delta": None}
    if base <= 0 or final <= 0:
        return {"status": "unrateable", "delta": None}
    delta = round((final - base) / base * 100.0, 1)
    if delta >= _RECORD_HIT_PCT:
        return {"status": "win", "delta": delta}
    if delta <= _RECORD_MISS_PCT:
        return {"status": "miss", "delta": delta}
    return {"status": "quiet", "delta": delta}


def _record_median(values) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return round(float(statistics.median(clean)), 1)


def _record_status_badge(status: str) -> str:
    label, tone = {
        "win": ("Win", "mint"),
        "quiet": ("Quiet", "amber"),
        "miss": ("Miss", "rose"),
        "open": ("Open", "blue"),
        "unrateable": ("No data", "dim"),
    }.get(status, ("—", "dim"))
    return _badge(label, tone)


def _track_record_data() -> dict:
    """Grade every alert from the last 30 days at a fixed 72h horizon.

    Baseline is the last snapshot at or before the alert; the outcome is
    the first snapshot between 72 and 96 hours after it. Calls younger
    than 72 hours stay Open and never enter the hit-rate denominator; a
    matured call with no comparable pair is Unrateable, not a miss.
    """
    now = _now()
    cutoff = now - timedelta(days=_RECORD_WINDOW_DAYS)
    alerts = []
    for alert in _alerted_rows():
        alerted_dt = _parse_ts(alert.get("first_alerted"))
        if alerted_dt is None or alerted_dt < cutoff:
            continue
        try:
            gid = int(alert["game_id"])
        except (KeyError, TypeError, ValueError):
            continue
        alerts.append((alert, gid, alerted_dt))

    series = _series_by_game(
        [gid for _alert, gid, _dt in alerts],
        hours=(_RECORD_WINDOW_DAYS + 1) * 24,
    )
    calls = []
    for alert, gid, alerted_dt in alerts:
        points = [
            point for point in series.get(gid, [])
            if point.get("dt") is not None and point.get("players") is not None
        ]
        baseline_point = None
        for point in points:
            if point["dt"] <= alerted_dt:
                baseline_point = point
            else:
                break
        latest_point = points[-1] if points else None

        horizon = alerted_dt + timedelta(hours=_RECORD_HORIZON_HOURS)
        grace_end = horizon + timedelta(hours=_RECORD_GRACE_HOURS)
        outcome_point = None
        for point in points:
            if point["dt"] < horizon:
                continue
            if point["dt"] <= grace_end:
                outcome_point = point
            break

        is_open = now < horizon
        so_far = None
        if is_open:
            status = "open"
            delta = None
            reason = None
            so_far = _pct_change(
                baseline_point["players"] if baseline_point else None,
                latest_point["players"] if latest_point else None,
            )
        else:
            graded = _grade_record_call(
                baseline_point["players"] if baseline_point else None,
                outcome_point["players"] if outcome_point else None,
            )
            status, delta = graded["status"], graded["delta"]
            if status == "unrateable":
                if baseline_point is None:
                    reason = "no baseline snapshot"
                elif outcome_point is None:
                    reason = "no 72h snapshot"
                else:
                    reason = "zero baseline"
            else:
                reason = None

        score = _as_int(alert.get("best_scout_score"))
        calls.append({
            "game_id": gid,
            "name": (alert.get("game_name") or "").strip() or f"Game {gid}",
            "alerted_dt": alerted_dt,
            "alerted": _fmt_ts(alerted_dt),
            "alerted_day": alerted_dt.strftime("%b %d"),
            "score": score,
            "baseline": baseline_point["players"] if baseline_point else None,
            "outcome": outcome_point["players"] if outcome_point else None,
            "outcome_dt": outcome_point["dt"] if outcome_point else None,
            "latest": latest_point["players"] if latest_point else None,
            "latest_dt": latest_point["dt"] if latest_point else None,
            "status": status,
            "delta": delta,
            "so_far": so_far,
            "reason": reason,
            "open_in_hours": (
                max(0.0, (horizon - now).total_seconds() / 3600.0)
                if is_open else 0.0
            ),
        })

    calls.sort(key=lambda c: c["alerted_dt"], reverse=True)
    graded = [c for c in calls if c["status"] in ("win", "quiet", "miss")]
    wins = sum(1 for c in graded if c["status"] == "win")
    quiet = sum(1 for c in graded if c["status"] == "quiet")
    misses = sum(1 for c in graded if c["status"] == "miss")
    hit_rate = round(wins / len(graded) * 100.0, 1) if graded else None
    median_move = _record_median([c["delta"] for c in graded])

    band_specs = (
        ("90+", 90, 1000),
        ("80–89", 80, 90),
        ("70–79", 70, 80),
        ("Below 70", 0, 70),
    )
    bands = []
    for label, low, high in band_specs:
        entries = [
            c for c in graded
            if c["score"] is not None and low <= c["score"] < high
        ]
        band_wins = sum(1 for c in entries if c["status"] == "win")
        bands.append({
            "label": label,
            "graded": len(entries),
            "wins": band_wins,
            "misses": sum(1 for c in entries if c["status"] == "miss"),
            "hit_rate": (
                round(band_wins / len(entries) * 100.0, 1) if entries else None
            ),
            "median": _record_median([c["delta"] for c in entries]),
        })

    top = [c for c in graded if c["score"] is not None and c["score"] >= 90]
    rest = [c for c in graded if c["score"] is not None and c["score"] < 90]
    edge = {
        "title": "Not enough graded calls to compare bands yet.",
        "detail": "The record needs at least two graded calls on each side.",
    }
    if len(top) >= 2 and len(rest) >= 2:
        top_median = _record_median([c["delta"] for c in top])
        rest_median = _record_median([c["delta"] for c in rest])
        difference = round((top_median or 0) - (rest_median or 0), 1)
        if difference >= 5:
            edge = {
                "title": "The 90+ band is holding up better.",
                "detail": (
                    f"Median 72h move {top_median:+.1f}% for 90+ calls "
                    f"vs {rest_median:+.1f}% below 90."
                ),
            }
        elif difference <= -5:
            edge = {
                "title": "Higher scores are not outperforming yet.",
                "detail": (
                    f"Median 72h move {top_median:+.1f}% for 90+ calls "
                    f"vs {rest_median:+.1f}% below 90."
                ),
            }
        else:
            edge = {
                "title": "No clear score edge yet.",
                "detail": (
                    f"Median 72h move {top_median:+.1f}% for 90+ calls "
                    f"vs {rest_median:+.1f}% below 90."
                ),
            }

    strongest = max(graded, key=lambda c: c["delta"], default=None)
    weakest = min(graded, key=lambda c: c["delta"], default=None)
    return {
        "calls": calls,
        "graded": graded,
        "wins": wins,
        "quiet": quiet,
        "misses": misses,
        "hit_rate": hit_rate,
        "median_move": median_move,
        "bands": bands,
        "edge": edge,
        "strongest": strongest,
        "weakest": weakest,
        "open": sum(1 for c in calls if c["status"] == "open"),
        "unrateable": sum(1 for c in calls if c["status"] == "unrateable"),
        "total": len(calls),
    }


def _game_detail(game_id: int) -> dict:
    gid = int(game_id)
    alert = next(
        (r for r in _alerted_rows() if int(r.get("game_id", -1)) == gid), None
    )
    watch = next(
        (r for r in _watched_rows() if int(r.get("game_id", -1)) == gid), None
    )
    project = next(
        (r for r in _portfolio_rows() if int(r.get("game_id", -1)) == gid), None
    )
    ai = _ai_map().get(gid)
    radar = _radar_map().get(gid)
    points = _series_by_game([gid], hours=24 * 30).get(gid, [])

    chart_rows = _rows(
        """
        SELECT sort_label, position, recorded_at
        FROM chart_positions
        WHERE game_id = ?
          AND id IN (
              SELECT MAX(id) FROM chart_positions
              WHERE game_id = ? AND recorded_at >= datetime('now','-30 days')
              GROUP BY sort_id
          )
        ORDER BY position ASC
        """,
        (gid, gid),
    )
    best_chart = None
    for row in chart_rows:
        try:
            position = int(row["position"])
        except (TypeError, ValueError):
            continue
        best_chart = position if best_chart is None else min(best_chart, position)

    name = (
        ((alert or {}).get("game_name") or "").strip()
        or ((project or {}).get("display_name") or "").strip()
        or ((watch or {}).get("game_name") or "").strip()
        or ((radar or {}).get("game_name") or "").strip()
        or f"Game {gid}"
    )

    latest = points[-1] if points else None
    latest_players = latest["players"] if latest else (
        (watch or {}).get("last_players")
    )
    latest_visits = latest["visits"] if latest else (
        (watch or {}).get("last_visits")
    )
    peak_30d = max(
        (p["players"] for p in points if isinstance(p.get("players"), (int, float))),
        default=None,
    )
    growth = {}
    for key, hours in _RANGE_HOURS:
        window = _window(points, hours)
        growth[key] = (
            _pct_change(window[0]["players"], window[-1]["players"])
            if len(window) >= 2 else None
        )

    ranges = {}
    for key, hours in _RANGE_HOURS:
        window = _window(points, hours)
        ranges[key] = [
            (p["dt"], p["players"]) for p in window
            if p.get("dt") is not None and p.get("players") is not None
        ]

    grade = None
    first_alerted = (alert or {}).get("first_alerted")
    if first_alerted and latest_players is not None:
        baseline_rows = _rows(
            """
            SELECT players FROM game_history
            WHERE game_id = ? AND timestamp <= ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (gid, first_alerted),
        )
        baseline = baseline_rows[0]["players"] if baseline_rows else None
        try:
            from scorecard import evaluate_call
            grade = evaluate_call(first_alerted, baseline, latest_players)
        except Exception as exc:  # grading is a bonus, never a blocker
            logger.debug("call grade failed: %s", exc)
            grade = None

    return {
        "game_id": gid,
        "name": name,
        "alert": alert,
        "watch": watch,
        "project": project,
        "ai": ai,
        "radar": radar,
        "points": points,
        "ranges": ranges,
        "growth": growth,
        "latest_players": latest_players,
        "latest_visits": latest_visits,
        "peak_30d": peak_30d,
        "first_alerted": first_alerted,
        "charts": chart_rows,
        "best_chart": best_chart,
        "grade": grade,
        "history_count": len(points),
        "last_seen": latest["timestamp"] if latest else (watch or {}).get("last_checked"),
    }


# ---------------------------------------------------------------------------
# Visual components
# ---------------------------------------------------------------------------

def sparkline_svg(players, width=140, height=40, color="#5eead4"):
    """Inline SVG sparkline; returns None for an empty series."""
    values = [
        float(p) for p in players
        if isinstance(p, (int, float)) and p is not None
    ]
    if not values:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    last_index = max(len(values) - 1, 1)
    coords = []
    for index, value in enumerate(values):
        x = round(index / last_index * (width - 4) + 2, 1)
        y = round(height - 4 - (value - lo) / span * (height - 10), 1)
        coords.append((x, y))
    points = " ".join(f"{x},{y}" for x, y in coords)
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="CCU trend">'
        f'<polygon fill="{color}" opacity="0.12" '
        f'points="0,{height} {points} {width},{height}"/>'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{points}"/>'
        f"</svg>"
    )


def _axis_time(dt: datetime, span_hours: float) -> str:
    if span_hours <= 36:
        return dt.strftime("%H:%M")
    if span_hours <= 24 * 10:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%b %d")


def chart_svg(points, ident: str, width=920, height=300, compact=False,
              benchmark=None):
    """A readable inline-SVG line chart for one game's CCU series.

    points: [(datetime, players), ...], oldest first. Returns None when
    there is nothing to draw. X is real time (gaps stay gaps); Y starts at
    zero. Includes grid, compact y labels, x labels, an area fill and a
    last-point marker. `benchmark` is an optional [(datetime, players)]
    series drawn as a dashed line on the same scale (already scaled to
    this game's starting value by the caller). `compact` draws a narrower
    viewBox for phones so axis text is not scaled into illegibility by the
    responsive width.
    """
    data = [
        (dt, float(value)) for dt, value in points
        if isinstance(dt, datetime) and isinstance(value, (int, float))
    ]
    if not data:
        return None

    left, right, top, bottom = (44, 10, 14, 24) if compact else (58, 18, 18, 30)
    plot_w = width - left - right
    plot_h = height - top - bottom
    start = data[0][0]
    end = data[-1][0]
    span_hours = max((end - start).total_seconds() / 3600.0, 0.0)

    bench_candidates = [
        (dt, float(value)) for dt, value in (benchmark or [])
        if isinstance(dt, datetime) and isinstance(value, (int, float))
        and dt <= end
    ]
    # Buckets are floored, so the first benchmark point can sit a few
    # minutes before the game's first reading. Keep it (clamped to the
    # left edge) so the dashed line starts with the series instead of
    # losing a point to a sub-bucket rounding difference.
    bench_before = [p for p in bench_candidates if p[0] < start]
    bench = ([bench_before[-1]] if bench_before else []) + [
        p for p in bench_candidates if p[0] >= start
    ]

    hi = max([value for _dt, value in data] + [value for _dt, value in bench])
    if hi <= 0:
        hi = 1.0
    magnitude = 10 ** math.floor(math.log10(hi))
    step = magnitude / 2 or 1
    ceiling = math.ceil(hi * 1.08 / step) * step
    if ceiling <= hi:
        ceiling += step

    def x_of(index, dt):
        if len(data) == 1 or end <= start:
            return left + plot_w / 2
        return left + (dt - start).total_seconds() / (end - start).total_seconds() * plot_w

    def y_of(value):
        return top + plot_h - (value / ceiling) * plot_h

    coords = [(round(x_of(i, dt), 1), round(y_of(v), 1)) for i, (dt, v) in enumerate(data)]
    polyline = " ".join(f"{x},{y}" for x, y in coords)
    area = (
        f"{coords[0][0]},{top + plot_h} {polyline} "
        f"{coords[-1][0]},{top + plot_h}"
    )

    bench_line = ""
    if len(bench) >= 2:
        bench_coords = [
            (
                round(max(left, min(width - right, x_of(i, dt))), 1),
                round(y_of(value), 1),
            )
            for i, (dt, value) in enumerate(bench)
        ]
        bench_points = " ".join(f"{x},{y}" for x, y in bench_coords)
        bench_line = (
            f'<polyline class="chart-bench" points="{bench_points}"/>'
        )

    gradient_id = re.sub(r"[^A-Za-z0-9_-]", "", f"grade-{ident}") or "grade"
    grid_lines = []
    labels = []
    for index in range(4):
        value = ceiling * index / 3
        y = round(y_of(value), 1)
        grid_lines.append(
            f'<line class="chart-grid" x1="{left}" y1="{y}" '
            f'x2="{width - right}" y2="{y}"/>'
        )
        labels.append(
            f'<text class="chart-axis" x="{left - 10}" y="{y + 4}" '
            f'text-anchor="end">{_esc(_fmt_compact(value))}</text>'
        )

    x_labels = []
    positions = (0, 1) if compact else (0, 1, 2)
    if end <= start:
        positions = (0,)
    for index in positions:
        share = index / (len(positions) - 1) if len(positions) > 1 else 0.5
        dt = start + (end - start) * share if end > start else start
        x = left + plot_w * share
        anchor = (
            "start" if index == 0 and len(positions) > 1
            else ("end" if index == len(positions) - 1 and len(positions) > 1
                  else "middle")
        )
        x_labels.append(
            f'<text class="chart-axis" x="{round(x, 1)}" y="{height - 7}" '
            f'text-anchor="{anchor}">{_esc(_axis_time(dt, span_hours))}</text>'
        )

    dots = []
    if len(data) <= 90:
        for index, (dt, value) in enumerate(data):
            x, y = coords[index]
            dots.append(
                f'<circle cx="{x}" cy="{y}" r="6" fill="transparent">'
                f'<title>{_esc(_fmt_ts(dt))} — {_fmt_int(value)} CCU</title>'
                f"</circle>"
            )
    last_x, last_y = coords[-1]
    return (
        f'<svg class="chart{" chart-compact" if compact else ""}" '
        f'viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="CCU history">'
        f"<defs><linearGradient id=\"{gradient_id}\" x1=\"0\" y1=\"0\" "
        f'x2="0" y2="1"><stop offset="0" stop-color="#5eead4" '
        f'stop-opacity="0.26"/><stop offset="1" stop-color="#5eead4" '
        f'stop-opacity="0"/></linearGradient></defs>'
        f"{''.join(grid_lines)}{''.join(labels)}"
        f'<polygon fill="url(#{gradient_id})" points="{area}"/>'
        f"{bench_line}"
        f'<polyline class="chart-line" points="{polyline}"/>'
        f'<circle class="chart-halo" cx="{last_x}" cy="{last_y}" r="8"/>'
        f'<circle class="chart-dot" cx="{last_x}" cy="{last_y}" r="4">'
        f"<title>Latest — {_fmt_int(data[-1][1])} CCU</title></circle>"
        f"{''.join(dots)}{''.join(x_labels)}</svg>"
    )


def _score(score) -> str:
    if not isinstance(score, (int, float)):
        return '<span class="muted">—</span>'
    width = max(4, min(100, int(score)))
    return (
        f'<span class="score"><span class="score-num">{int(score)}</span>'
        f'<span class="score-bar"><i style="width:{width}%"></i></span></span>'
    )


def _badge(text, tone="dim") -> str:
    return f'<span class="badge {tone}">{_esc(text)}</span>'


def _empty(text, hint="", strong=None) -> str:
    title = strong if strong is not None else text
    hint_html = f"<span>{_esc(hint)}</span>" if hint else ""
    if strong is not None:
        return (
            f'<div class="empty"><strong>{_esc(title)}</strong>'
            f"<span>{_esc(text)}</span>{hint_html}</div>"
        )
    return f'<div class="empty"><strong>{_esc(text)}</strong>{hint_html}</div>'


def _flag_badges(row) -> str:
    badges = []
    if "alerted" in row["flags"]:
        badges.append(_badge("Alerted", "mint"))
    if row.get("chart_best") is not None:
        badges.append(_badge(f"Chart #{row['chart_best']}", "amber"))
    if "watched" in row["flags"]:
        badges.append(_badge("Watched", "blue"))
    if "portfolio" in row["flags"]:
        badges.append(_badge("Portfolio", "violet"))
    return "".join(badges)


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#06090d;--panel:#0d141c;--panel-2:#111a24;--line:rgba(148,163,184,.14);
  --line-strong:rgba(148,163,184,.26);--text:#edf3f8;--muted:#8b9bad;
  --dim:#5f7082;--mint:#5eead4;--mint-soft:rgba(94,234,212,.12);
  --blue:#7aa2ff;--amber:#f5b657;--rose:#fb7185;--violet:#c4a5ff;
  --green:#4ade80;--radius:16px;
}
[hidden]{display:none!important}
html{color-scheme:dark;scroll-behavior:smooth}
body{
  margin:0;color:var(--text);font-size:15px;line-height:1.55;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,
    Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;
  background:
    radial-gradient(1100px 620px at 72% -12%,rgba(94,234,212,.09),transparent 62%),
    radial-gradient(900px 520px at 8% -18%,rgba(122,162,255,.10),transparent 58%),
    var(--bg);
  min-height:100vh;
}
a{color:inherit;text-decoration:none}
a:hover{color:var(--mint)}
button,input,select{font:inherit}
::selection{background:rgba(94,234,212,.25);color:#fff}
.shell{width:100%;max-width:1240px;margin:0 auto;padding:0 28px}
.skip{position:absolute;left:-999px;top:0;background:var(--mint);color:#04110d;
  padding:.6rem 1rem;border-radius:0 0 8px 0;z-index:99}
.skip:focus{left:0}
.topbar{position:sticky;top:0;z-index:40;border-bottom:1px solid var(--line);
  background:rgba(6,9,13,.82);backdrop-filter:blur(16px)}
.topbar-inner{display:flex;align-items:center;gap:2rem;height:66px}
.brand{display:flex;align-items:center;gap:.7rem;font-weight:700;
  letter-spacing:-.03em;font-size:1.03rem;white-space:nowrap}
.brand-mark{width:32px;height:32px;border-radius:10px;display:grid;
  place-items:center;color:#04110d;
  background:linear-gradient(135deg,var(--mint),#63b3ff);
  box-shadow:0 0 26px rgba(94,234,212,.24)}
.brand-mark svg{width:18px;height:18px}
.brand small{display:block;font-size:.66rem;font-weight:600;letter-spacing:.14em;
  text-transform:uppercase;color:var(--dim);line-height:1.2}
.nav{display:flex;align-items:center;gap:.2rem;margin-left:.5rem}
.nav a{padding:.45rem .75rem;border-radius:9px;color:var(--muted);
  font-size:.86rem;font-weight:550}
.nav a:hover{color:var(--text);background:rgba(255,255,255,.04)}
.nav a[aria-current="page"]{color:var(--text);background:rgba(255,255,255,.06)}
.top-right{margin-left:auto;display:flex;align-items:center;gap:.9rem}
.live{display:inline-flex;align-items:center;gap:.45rem;padding:.3rem .65rem;
  border:1px solid rgba(94,234,212,.24);border-radius:999px;
  background:rgba(94,234,212,.07);font-size:.7rem;font-weight:650;
  letter-spacing:.1em;text-transform:uppercase;color:var(--mint)}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--mint);
  box-shadow:0 0 0 4px rgba(94,234,212,.12);animation:pulse 2.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.top-link{font-size:.8rem;color:var(--muted)}
.top-link[aria-current="page"]{color:var(--text);font-weight:650}
.footer-links{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem 1rem;
  font-size:.72rem;color:var(--dim)}
.footer-links a{color:var(--muted)}
.footer-links a:hover{color:var(--mint)}
.hero{position:relative;display:grid;grid-template-columns:minmax(0,1.3fr) minmax(300px,.7fr);
  gap:34px;align-items:center;padding:58px 0 36px}
.hero::before{content:"";position:absolute;inset:-60px 0 auto;
  height:340px;z-index:-1;opacity:.5;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:52px 52px;
  mask-image:radial-gradient(ellipse at 60% 0,#000 0%,transparent 72%);
  -webkit-mask-image:radial-gradient(ellipse at 60% 0,#000 0%,transparent 72%)}
.eyebrow{display:flex;align-items:center;gap:.55rem;margin:0 0 1rem;
  font-size:.7rem;font-weight:700;letter-spacing:.18em;text-transform:uppercase;
  color:var(--mint)}
.eyebrow::before{content:"";width:22px;height:1px;background:var(--mint);
  opacity:.7}
h1{font-size:clamp(2.2rem,4.4vw,3.5rem);line-height:1.03;
  letter-spacing:-.045em;margin:0 0 1rem;font-weight:750;max-width:18ch}
.grad{background:linear-gradient(92deg,var(--mint),#8db9ff 70%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.lede{margin:0;max-width:58ch;color:var(--muted);font-size:1.02rem;
  line-height:1.65}
.hero-actions{display:flex;flex-wrap:wrap;align-items:center;gap:.7rem;
  margin-top:1.6rem}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.45rem;
  padding:.68rem 1.05rem;border-radius:10px;font-size:.86rem;font-weight:650;
  border:1px solid transparent;transition:transform .15s ease,background .15s ease}
.btn:hover{transform:translateY(-1px)}
.btn-primary{background:var(--mint);color:#04211b}
.btn-primary:hover{background:#8af2e0;color:#04211b}
.btn-ghost{border-color:var(--line-strong);color:var(--text);
  background:rgba(255,255,255,.02)}
.btn-ghost:hover{background:rgba(255,255,255,.05);color:var(--text)}
.hero-note{font-size:.76rem;color:var(--dim)}
.signal{position:relative;overflow:hidden;padding:20px;border-radius:20px;
  border:1px solid rgba(94,234,212,.18);background:
    radial-gradient(420px 200px at 100% 0,rgba(94,234,212,.12),transparent 65%),
    linear-gradient(160deg,rgba(255,255,255,.045),transparent 55%),var(--panel);
  box-shadow:0 30px 70px -38px rgba(0,0,0,.9)}
.signal-head{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:.4rem}
.signal-title{font-size:.68rem;font-weight:700;letter-spacing:.16em;
  text-transform:uppercase;color:var(--dim)}
.signal-row{display:grid;grid-template-columns:1fr auto;gap:.1rem .8rem;
  padding:.85rem 0;border-top:1px solid var(--line)}
.signal-row:first-of-type{border-top:0}
.signal-label{font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--dim);grid-column:1/-1}
.signal-name{font-weight:650;font-size:.94rem;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.signal-metric{font-weight:700;font-size:.9rem;text-align:right}
.signal-metric small{display:block;font-weight:500;color:var(--dim);font-size:.7rem}
.signal-foot{display:flex;justify-content:space-between;gap:1rem;padding-top:.85rem;
  margin-top:.2rem;border-top:1px solid var(--line);font-size:.72rem;color:var(--dim)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;
  margin:4px 0 24px}
.stat{position:relative;overflow:hidden;padding:17px 18px 15px;border-radius:14px;
  border:1px solid var(--line);background:linear-gradient(180deg,
    rgba(255,255,255,.035),rgba(255,255,255,0) 70%),var(--panel)}
.stat::after{content:"";position:absolute;left:18px;right:18px;bottom:0;height:1px;
  background:linear-gradient(90deg,var(--mint),transparent)}
.stat-label{font-size:.68rem;font-weight:650;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim)}
.stat-value{font-size:1.85rem;font-weight:720;letter-spacing:-.04em;
  line-height:1.25;margin-top:.15rem}
.stat-foot{font-size:.73rem;color:var(--muted)}
.layout-2{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);
  gap:16px;align-items:start}
.layout-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  gap:16px;margin-top:16px;align-items:start}
.panel{border:1px solid var(--line);border-radius:var(--radius);
  background:linear-gradient(180deg,rgba(255,255,255,.022),transparent 40%),
    var(--panel);box-shadow:0 24px 60px -45px rgba(0,0,0,.9);
  overflow:hidden}
.panel-head{display:flex;align-items:flex-end;justify-content:space-between;
  gap:1rem;padding:18px 20px 12px}
.panel-title{font-size:.95rem;font-weight:680;letter-spacing:-.01em;margin:0}
.panel-sub{margin:.15rem 0 0;font-size:.76rem;color:var(--muted)}
.panel-link{font-size:.75rem;color:var(--muted);white-space:nowrap}
.panel-link:hover{color:var(--mint)}
.panel-body{padding:0 20px 18px}
.panel-body.pad{padding:16px 20px}
.record-bar{display:flex;height:18px;margin-top:4px;border-radius:999px;
  overflow:hidden;border:1px solid var(--line);background:rgba(255,255,255,.02)}
.record-seg{display:block;height:100%;min-width:3px}
.record-seg.win{background:linear-gradient(90deg,var(--mint),#63e6c8)}
.record-seg.quiet{background:linear-gradient(90deg,var(--amber),#f7c77c)}
.record-seg.miss{background:linear-gradient(90deg,var(--rose),#fda4af)}
.record-legend{display:flex;flex-wrap:wrap;align-items:center;gap:.55rem 1.1rem;
  margin-top:.85rem;font-size:.74rem;color:var(--muted)}
.record-key{display:inline-flex;align-items:center;gap:.38rem}
.record-key b{color:var(--text);font-weight:650}
.record-swatch{display:inline-block;width:8px;height:8px;border-radius:50%}
.record-swatch.win{background:var(--mint)}
.record-swatch.quiet{background:var(--amber)}
.record-swatch.miss{background:var(--rose)}
.record-scatter{display:block;width:100%;height:auto;margin-top:2px}
.record-scatter .axis{stroke:rgba(148,163,184,.10);stroke-width:1}
.record-scatter .zero{stroke:rgba(148,163,184,.35);stroke-width:1;
  stroke-dasharray:4 4}
.record-scatter .axis-label{fill:var(--dim);font-size:10px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.record-scatter .dot{stroke:var(--panel);stroke-width:2}
.record-scatter .dot.win{fill:var(--mint)}
.record-scatter .dot.quiet{fill:var(--amber)}
.record-scatter .dot.miss{fill:var(--rose)}
.band-table{font-size:.8rem}
.band-table td{padding:9px 6px}
.band-name{font-weight:650}
.band-rate{display:flex;align-items:center;gap:.5rem;min-width:82px}
.band-meter{flex:1;height:5px;min-width:34px;border-radius:999px;
  overflow:hidden;background:rgba(148,163,184,.14)}
.band-meter i{display:block;height:100%;border-radius:inherit;
  background:linear-gradient(90deg,var(--mint),#63b3ff)}
.record-edge{display:flex;flex-direction:column;gap:.18rem;margin-top:14px;
  padding:12px 13px;border:1px solid rgba(94,234,212,.16);border-radius:10px;
  background:rgba(94,234,212,.05);font-size:.76rem;color:var(--muted)}
.record-edge strong{color:var(--text);font-weight:650;font-size:.82rem}
.record-note{margin:.75rem 0 0;font-size:.7rem;color:var(--dim)}
.record-so-far{display:block;font-size:.62rem;color:var(--dim);letter-spacing:.04em}
.record-time{display:block;font-size:.62rem;color:var(--dim)}
.record-table td{font-size:.8rem}
.table{width:100%;border-collapse:collapse}
.table th{font-size:.64rem;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim);text-align:left;
  padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.table td{padding:12px 10px;border-bottom:1px solid rgba(148,163,184,.08);
  vertical-align:middle}
.table tr:last-child td{border-bottom:0}
.table tbody tr:hover td{background:rgba(255,255,255,.018)}
.num{text-align:right;white-space:nowrap}
.game-link{display:flex;flex-direction:column;gap:1px;min-width:0}
.game-name{font-weight:620;font-size:.88rem;color:var(--text);line-height:1.3}
.game-link:hover .game-name{color:var(--mint)}
.game-id{font-size:.68rem;color:var(--dim);letter-spacing:.02em}
.muted{color:var(--muted)}
.dim{color:var(--dim)}
.up{color:var(--green)}
.down{color:var(--rose)}
.flat{color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:.3rem;padding:.16rem .48rem;
  border-radius:999px;font-size:.66rem;font-weight:650;line-height:1.6;
  white-space:nowrap;border:1px solid transparent}
.badge.mint{color:var(--mint);background:var(--mint-soft);
  border-color:rgba(94,234,212,.22)}
.badge.blue{color:var(--blue);background:rgba(122,162,255,.1);
  border-color:rgba(122,162,255,.22)}
.badge.amber{color:var(--amber);background:rgba(245,182,87,.1);
  border-color:rgba(245,182,87,.22)}
.badge.rose{color:var(--rose);background:rgba(251,113,133,.1);
  border-color:rgba(251,113,133,.22)}
.badge.violet{color:var(--violet);background:rgba(196,165,255,.1);
  border-color:rgba(196,165,255,.22)}
.badge.dim{color:var(--muted);background:rgba(148,163,184,.08);
  border-color:rgba(148,163,184,.16)}
.score{display:inline-flex;align-items:center;gap:.55rem}
.score-num{font-weight:720;font-size:.86rem;min-width:1.4rem}
.score-bar{width:58px;height:4px;border-radius:999px;overflow:hidden;
  background:rgba(255,255,255,.07)}
.score-bar i{display:block;height:100%;border-radius:inherit;
  background:linear-gradient(90deg,var(--mint),#78a8ff)}
.list{list-style:none;margin:0;padding:0}
.list li{display:flex;align-items:center;gap:.7rem;padding:.62rem 0;
  border-bottom:1px solid rgba(148,163,184,.08)}
.list li:last-child{border-bottom:0}
.list-main{min-width:0;flex:1}
.list-name{display:block;font-size:.83rem;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.list-meta{display:block;font-size:.68rem;color:var(--dim)}
.list-side{text-align:right;white-space:nowrap}
.mover-spark{flex:0 0 92px}
.scroll{overflow-x:auto}
.empty{margin:6px 0 2px;padding:30px 18px;text-align:center;border-radius:12px;
  border:1px dashed var(--line-strong);background:rgba(255,255,255,.012);
  color:var(--muted);font-size:.86rem}
.empty strong{display:block;color:var(--text);font-weight:620;margin-bottom:.25rem}
.empty span{display:block}
.footnote{font-size:.7rem;color:var(--dim);margin:.7rem 0 0}
.projects{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
  gap:12px;padding:4px 20px 20px}
.project{display:flex;flex-direction:column;gap:.75rem;padding:15px;
  border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.018);
  transition:border-color .15s ease,transform .15s ease}
.project:hover{border-color:var(--line-strong);transform:translateY(-1px)}
.project-top{display:flex;align-items:center;gap:.7rem;min-width:0}
.monogram{width:44px;height:44px;border-radius:11px;display:grid;place-items:center;
  font-weight:750;font-size:1rem;color:#06251f;
  background:linear-gradient(135deg,var(--mint),#7aa2ff)}
.project-name{font-weight:650;font-size:.9rem;line-height:1.25;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.project-role{font-size:.66rem;color:var(--dim);text-transform:uppercase;
  letter-spacing:.08em}
.project-stats{display:flex;align-items:flex-end;justify-content:space-between;
  gap:.8rem}
.project-ccu{font-size:1.45rem;font-weight:720;letter-spacing:-.03em;line-height:1}
.project-ccu small{font-size:.66rem;color:var(--dim);font-weight:600;
  letter-spacing:.06em;margin-left:.2rem}
.project-growth{font-size:.72rem;color:var(--muted)}
.page-head{padding:42px 0 20px}
.page-head h1{font-size:clamp(1.8rem,3.4vw,2.6rem);max-width:none}
.page-head .lede{max-width:66ch;font-size:.95rem}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:.6rem;
  padding:14px 16px;border-bottom:1px solid var(--line);
  background:rgba(255,255,255,.012)}
.search{position:relative;flex:1 1 260px;min-width:200px}
.search svg{position:absolute;left:.72rem;top:50%;transform:translateY(-50%);
  width:15px;height:15px;color:var(--dim);pointer-events:none}
.search input{width:100%;padding:.58rem .75rem .58rem 2.1rem;border-radius:9px;
  border:1px solid var(--line);background:#080f15;color:var(--text);
  font-size:.84rem;outline:none}
.search input:focus{border-color:rgba(94,234,212,.45);
  box-shadow:0 0 0 3px rgba(94,234,212,.08)}
.chips{display:flex;flex-wrap:wrap;gap:.35rem}
.chip{padding:.36rem .65rem;border-radius:999px;border:1px solid var(--line);
  background:transparent;color:var(--muted);font-size:.72rem;font-weight:600;
  cursor:pointer;transition:all .12s ease}
.chip:hover{color:var(--text);border-color:var(--line-strong)}
.chip.active{color:var(--mint);background:var(--mint-soft);
  border-color:rgba(94,234,212,.32)}
select{padding:.52rem .65rem;border-radius:9px;border:1px solid var(--line);
  background:#080f15;color:var(--text);font-size:.78rem;outline:none}
.toolbar-note{margin-left:auto;font-size:.72rem;color:var(--dim);
  white-space:nowrap}
.chip.export{color:var(--mint);border-color:rgba(94,234,212,.3);
  background:rgba(94,234,212,.07)}
.chip.export:hover{background:rgba(94,234,212,.14);color:var(--mint)}
.compare-form{display:flex;flex-wrap:wrap;align-items:flex-end;gap:.7rem;
  padding:16px 18px}
.compare-field{display:flex;flex-direction:column;gap:.28rem;flex:1 1 220px;
  min-width:0}
.compare-field span{font-size:.63rem;font-weight:650;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim)}
.compare-field select{width:100%}
.compare-vs{padding:.5rem .1rem;color:var(--dim);font-weight:700;
  font-size:.78rem;text-transform:uppercase;letter-spacing:.1em}
.compare-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
  gap:16px;margin-top:16px;align-items:start}
.compare-grid.single{grid-template-columns:1fr}
.compare-col{min-width:0}
.compare-head{display:flex;align-items:center;gap:.75rem;padding:2px 2px 12px}
.compare-head h2{font-size:1.12rem;margin:0;letter-spacing:-.02em;line-height:1.25}
.compare-head .game-meta{font-size:.71rem}
.game-mark.sm{width:44px;height:44px;border-radius:12px;font-size:.95rem}
.compare-link{display:inline-flex;margin-top:10px}
.compare-table td:nth-child(2),.compare-table td:nth-child(3){width:34%}
.compare-table td:first-child{color:var(--muted);font-size:.76rem}
.compare-table .best{color:var(--mint);font-weight:700}
.compare-note{margin:.65rem 0 0;font-size:.72rem;color:var(--dim)}
.crumbs{display:flex;align-items:center;gap:.5rem;font-size:.76rem;
  color:var(--dim);padding-top:26px}
.crumbs a{color:var(--muted)}
.crumbs a:hover{color:var(--mint)}
.game-head{display:flex;align-items:flex-start;gap:1rem;flex-wrap:wrap;
  padding:18px 0 24px}
.game-mark{width:64px;height:64px;border-radius:18px;display:grid;place-items:center;
  font-size:1.35rem;font-weight:780;color:#05221c;flex:0 0 auto;
  background:linear-gradient(135deg,var(--mint),#7aa2ff);
  box-shadow:0 0 40px rgba(94,234,212,.15)}
.game-head-main{flex:1 1 320px;min-width:0}
.game-head h1{font-size:clamp(1.7rem,3.2vw,2.45rem);margin:0 0 .35rem;
  max-width:none}
.game-meta{display:flex;flex-wrap:wrap;align-items:center;gap:.55rem;
  font-size:.76rem;color:var(--dim)}
.game-meta a{color:var(--muted);text-decoration:underline;
  text-decoration-color:rgba(148,163,184,.3);text-underline-offset:3px}
.game-meta a:hover{color:var(--mint)}
.game-actions{display:flex;flex-wrap:wrap;gap:.5rem}
.detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(290px,350px);
  gap:16px;align-items:start}
.rail{display:flex;flex-direction:column;gap:16px}
.mini-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);
  border-radius:14px;overflow:hidden;margin-bottom:16px}
.mini{padding:14px 16px;background:var(--panel)}
.mini-label{font-size:.62rem;font-weight:650;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
.mini-value{font-size:1.2rem;font-weight:700;letter-spacing:-.02em;
  margin-top:.1rem}
.mini-value small{font-size:.66rem;color:var(--dim);font-weight:600;
  letter-spacing:.04em}
.read{border:1px solid rgba(94,234,212,.2);border-radius:16px;
  padding:18px 20px 19px;margin:0 0 16px;
  background:
    radial-gradient(520px 180px at 100% 0,rgba(94,234,212,.09),transparent 68%),
    linear-gradient(160deg,rgba(255,255,255,.035),transparent 60%),var(--panel)}
.read-top{display:flex;align-items:center;justify-content:space-between;
  gap:1rem}
.read-label{font-size:.66rem;font-weight:700;letter-spacing:.16em;
  text-transform:uppercase;color:var(--mint)}
.read h2{font-size:clamp(1.12rem,2.1vw,1.5rem);line-height:1.25;
  letter-spacing:-.03em;margin:.55rem 0 .4rem;max-width:38ch}
.read p{margin:0;color:var(--muted);font-size:.85rem;line-height:1.65;
  max-width:100ch}
.timeline-panel{margin-top:16px}
.timeline{list-style:none;margin:0;padding:.15rem 0 0}
.timeline li{position:relative;margin-left:3px;padding:0 0 .95rem 1.3rem;
  border-left:1px solid rgba(148,163,184,.16)}
.timeline li:last-child{border-left-color:transparent;padding-bottom:.1rem}
.tl-dot{position:absolute;left:-5px;top:.42rem;width:9px;height:9px;
  border-radius:50%;background:var(--muted);box-shadow:0 0 0 3px var(--panel)}
.tl.mint .tl-dot{background:var(--mint)}
.tl.blue .tl-dot{background:var(--blue)}
.tl.rose .tl-dot{background:var(--rose)}
.tl.amber .tl-dot{background:var(--amber)}
.tl.violet .tl-dot{background:var(--violet)}
.tl-head{display:flex;justify-content:space-between;align-items:baseline;
  gap:1rem}
.tl-title{font-size:.8rem;font-weight:650}
.tl-head time{font-size:.66rem;color:var(--dim);white-space:nowrap}
.tl-detail{font-size:.74rem;color:var(--muted);margin-top:.1rem}
.chart-head{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:.7rem;padding:16px 18px 8px}
.tabs{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--line);
  border-radius:999px;background:rgba(0,0,0,.18)}
.tab{border:0;background:transparent;color:var(--muted);font-size:.72rem;
  font-weight:600;padding:.3rem .65rem;border-radius:999px;cursor:pointer}
.tab[aria-selected="true"]{background:rgba(94,234,212,.14);color:var(--mint)}
.chart-wrap{padding:2px 10px 12px}
.chart-wrap svg{display:block;width:100%;height:auto}
.chart-narrow{display:none}
.spark{display:block}
.chart-grid{stroke:rgba(148,163,184,.13);stroke-width:1;stroke-dasharray:3 5}
.chart-axis{fill:#6d7f91;font-size:11px;font-family:ui-sans-serif,system-ui,sans-serif}
.chart-line{fill:none;stroke:#5eead4;stroke-width:2.1;stroke-linecap:round;
  stroke-linejoin:round}
.chart-bench{fill:none;stroke:var(--blue);stroke-width:1.7;
  stroke-dasharray:5 5;stroke-linecap:round;opacity:.9}
.chart-key{display:flex;flex-wrap:wrap;gap:.35rem 1.1rem;
  padding:0 18px 9px;font-size:.68rem;color:var(--dim)}
.chart-key span{display:inline-flex;align-items:center;gap:.4rem}
.key-solid{width:16px;height:2px;border-radius:2px;background:var(--mint)}
.key-dash{width:16px;height:0;border-top:2px dashed var(--blue)}
.chart-basis{margin:0;padding:0 18px 8px;font-size:.66rem;color:var(--dim)}
.chart-halo{fill:rgba(94,234,212,.15)}
.chart-dot{fill:#5eead4;stroke:#06231e;stroke-width:1.5}
.chart-empty{padding:56px 20px}
.kv{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;
  padding:.56rem 0;border-bottom:1px solid rgba(148,163,184,.08);
  font-size:.8rem}
.kv:last-child{border-bottom:0}
.kv-k{color:var(--muted)}
.kv-v{font-weight:620;text-align:right;white-space:nowrap}
.verdict{padding:13px 14px;border-radius:12px;border:1px solid var(--line);
  background:rgba(255,255,255,.02);margin-bottom:.8rem}
.verdict-top{display:flex;align-items:center;justify-content:space-between;gap:.6rem}
.verdict-name{font-weight:700;font-size:.92rem}
.confidence{height:4px;border-radius:999px;background:rgba(255,255,255,.07);
  margin-top:.6rem;overflow:hidden}
.confidence i{display:block;height:100%;border-radius:inherit;
  background:linear-gradient(90deg,var(--mint),#7aa2ff)}
.bullets{list-style:none;margin:.65rem 0 0;padding:0}
.bullets li{position:relative;padding-left:1rem;margin:.35rem 0;
  font-size:.78rem;color:var(--muted);line-height:1.45}
.bullets li::before{content:"";position:absolute;left:0;top:.5em;width:4px;
  height:4px;border-radius:50%;background:var(--mint)}
.recommendation{margin:.75rem 0 0;padding-top:.7rem;
  border-top:1px solid rgba(148,163,184,.1);font-size:.79rem;
  color:var(--text);line-height:1.5}
.footer{border-top:1px solid var(--line);margin-top:52px;padding:22px 0 34px}
.footer-inner{display:flex;justify-content:space-between;gap:1.2rem;
  flex-wrap:wrap;font-size:.72rem;color:var(--dim)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.68rem}
.market-hero{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);
  gap:28px;align-items:stretch;padding:48px 0 22px}
.market-copy{display:flex;flex-direction:column;justify-content:center;min-width:0}
.market-copy h1{max-width:16ch}
.market-call{display:flex;align-items:center;gap:1.1rem;margin-top:1.7rem}
.market-median{font-size:clamp(2rem,4vw,2.85rem);font-weight:760;
  letter-spacing:-.05em;line-height:1;font-variant-numeric:tabular-nums}
.market-median.mint{color:var(--mint)}
.market-median.blue{color:var(--blue)}
.market-median.amber{color:var(--amber)}
.market-median.rose{color:var(--rose)}
.market-median.dim{color:var(--dim)}
.market-call-note{font-size:.72rem;color:var(--dim);margin-top:.4rem}
.breadth-panel{display:flex;flex-direction:column;justify-content:center}
.breadth + .breadth{margin-top:1.15rem}
.breadth-top{display:flex;justify-content:space-between;align-items:baseline;
  gap:.8rem;font-size:.72rem;color:var(--muted);margin-bottom:.4rem}
.breadth-top b{color:var(--text);font-weight:650}
.breadth-track{display:flex;gap:2px;height:9px;border-radius:999px;
  overflow:hidden;background:rgba(148,163,184,.12)}
.breadth-track i{display:block;height:100%}
.b-up{background:var(--mint)}
.b-flat{background:rgba(148,163,184,.45)}
.b-down{background:var(--rose)}
.breadth-scale{display:flex;justify-content:space-between;font-size:.66rem;
  color:var(--dim);margin-top:.34rem}
.breadth-legend{display:flex;flex-wrap:wrap;gap:.4rem 1rem;margin-top:1rem;
  padding-top:.85rem;border-top:1px solid rgba(148,163,184,.1);
  font-size:.68rem;color:var(--dim)}
.breadth-legend span{display:inline-flex;align-items:center;gap:.35rem}
.breadth-legend i{width:7px;height:7px;border-radius:2px;display:inline-block}
.hist{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:6px;
  align-items:end;height:150px;padding-top:.3rem}
.hist-col{display:flex;flex-direction:column;justify-content:flex-end;
  height:100%;gap:.22rem;text-align:center;min-width:0}
.hist-count{font-size:.64rem;color:var(--muted);line-height:1}
.hist-track{flex:1;display:flex;align-items:flex-end;justify-content:center}
.hist-bar{display:block;width:100%;max-width:30px;border-radius:4px 4px 2px 2px}
.hist-bar.rose{background:linear-gradient(180deg,var(--rose),rgba(251,113,133,.3))}
.hist-bar.amber{background:linear-gradient(180deg,var(--amber),rgba(245,182,87,.28))}
.hist-bar.mint{background:linear-gradient(180deg,var(--mint),rgba(94,234,212,.3))}
.hist-label{font-size:.64rem;color:var(--dim);white-space:nowrap;
  letter-spacing:-.02em}
.market-table td:first-child{width:52%}
.market-table td,.market-table th{padding-top:.5rem;padding-bottom:.5rem}
.market-empty{margin:.4rem 0;font-size:.78rem;color:var(--dim);line-height:1.5}
.market-foot{margin:.7rem 0 0;font-size:.68rem;color:var(--dim)}
@media (max-width:980px){
  .market-hero{grid-template-columns:1fr;padding-top:36px;gap:20px}
  .market-copy h1{max-width:20ch}
}
@media (max-width:680px){
  .market-hero{padding-top:28px}
  .hist{height:128px;gap:3px}
  .hist-label{font-size:.6rem}
  .market-table td:first-child{width:auto}
}
@media (max-width:980px){
  .hero{grid-template-columns:1fr;padding-top:40px}
  .layout-2,.layout-3{grid-template-columns:1fr}
  .detail{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .rail{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
  .compare-grid{grid-template-columns:1fr}
}
@media (max-width:680px){
  .shell{padding:0 16px}
  .topbar-inner{height:58px;gap:.8rem}
  .nav a{padding:.35rem .5rem;font-size:.8rem}
  .top-link{display:none}
  .live{display:none}
  .brand-text{display:none}
  .stats{grid-template-columns:1fr 1fr;gap:8px}
  .mini-stats{grid-template-columns:1fr 1fr}
  .rail{grid-template-columns:1fr}
  .hide-sm{display:none}
  .hero{padding:30px 0 22px;gap:22px}
  .hero-actions .btn{width:auto;flex:1 1 auto}
  .hero-note,.signal{display:none}
  .chart-wide{display:none}
  .chart-narrow{display:block}
  .read{padding:16px}
  .read h2{max-width:none}
  .compare-form{padding:14px}
  .compare-vs{display:none}
  .compare-form .btn{flex:1 1 auto}
  .toolbar-note{margin-left:0}
  .panel-head{padding:15px 15px 10px}
  .panel-body{padding:0 15px 15px}
  .projects{padding:0 15px 15px}
  .record-table .score-bar{display:none}
  .band-table .band-meter{display:none}
  .band-rate{min-width:58px}
}
@media (max-width:520px){
  .topbar-inner{height:auto;flex-wrap:wrap;padding-top:8px;gap:.3rem .7rem}
  .nav{order:3;width:100%;margin-left:0;padding-bottom:5px;gap:.1rem;
    overflow-x:auto;scrollbar-width:none}
  .nav::-webkit-scrollbar{display:none}
  .nav a{padding:.3rem .42rem;font-size:.74rem}
}
@media (max-width:380px){
  .topbar-inner{gap:.35rem}
  .brand-mark{width:28px;height:28px;border-radius:8px}
  .nav{margin-left:0;gap:0}
  .nav a{padding:.28rem .22rem;font-size:.7rem}
  .hide-xs{display:none}
  .table th,.table td{padding-left:6px;padding-right:6px}
  .record-table th,.record-table td,.band-table th,.band-table td{
    padding-left:3px;padding-right:3px}
}
"""

_UI_JS = r"""
(function(){
  // ---- game index: search, chips, sort ----
  var index = document.querySelector('[data-index]');
  if (index) {
    var rows = Array.prototype.slice.call(
      index.querySelectorAll('tbody tr[data-game]')
    );
    var search = index.querySelector('[data-search]');
    var chips = Array.prototype.slice.call(index.querySelectorAll('[data-filter]'));
    var sort = index.querySelector('[data-sort]');
    var count = index.querySelector('[data-count]');
    var noResults = index.querySelector('[data-no-results]');
    var currentFilter = 'all';

    function matchesFilter(row, filter) {
      if (filter === 'all') return true;
      return (' ' + (row.dataset.flags || '') + ' ').indexOf(' ' + filter + ' ') !== -1;
    }
    function apply() {
      var query = (search && search.value || '').toLowerCase().trim();
      var shown = 0;
      rows.forEach(function(row){
        var haystack = ((row.dataset.name || '') + ' ' + (row.dataset.id || '')).toLowerCase();
        var visible = (!query || haystack.indexOf(query) !== -1) &&
                      matchesFilter(row, currentFilter);
        row.hidden = !visible;
        if (visible) shown += 1;
      });
      if (count) count.textContent = String(shown);
      if (noResults) noResults.hidden = shown !== 0 || rows.length === 0;
    }
    if (search) search.addEventListener('input', apply);
    chips.forEach(function(chip){
      chip.addEventListener('click', function(){
        chips.forEach(function(other){ other.classList.remove('active'); });
        chip.classList.add('active');
        currentFilter = chip.dataset.filter || 'all';
        apply();
      });
    });
    if (sort) {
      sort.addEventListener('change', function(){
        var key = sort.value;
        var body = index.querySelector('tbody');
        rows.sort(function(a, b){
          if (key === 'name') {
            return (a.dataset.name || '').localeCompare(b.dataset.name || '');
          }
          if (key === 'recent') {
            return (b.dataset.recent || '').localeCompare(a.dataset.recent || '');
          }
          var av = Number(a.dataset[key]);
          var bv = Number(b.dataset[key]);
          if (!isFinite(av)) av = -1e15;
          if (!isFinite(bv)) bv = -1e15;
          return bv - av;
        });
        rows.forEach(function(row){ body.appendChild(row); });
      });
    }

    // Export exactly what the current search + filter is showing.
    var exportButton = index.querySelector('[data-export]');
    function csvCell(value) {
      return '"' + String(value == null ? '' : value).replace(/"/g, '""') + '"';
    }
    if (exportButton) {
      exportButton.addEventListener('click', function(){
        var visible = rows.filter(function(row){ return !row.hidden; });
        if (!visible.length) return;
        var lines = [['game_id','name','scout_score','ccu','7d_growth_pct',
          'alerts','last_seen','signals'].join(',')];
        visible.forEach(function(row){
          var growth = Number(row.dataset.growth) <= -1e8 ? '' : row.dataset.growth;
          var score = row.dataset.score === '-1' ? '' : row.dataset.score;
          var ccu = row.dataset.ccu === '-1' ? '' : row.dataset.ccu;
          lines.push([
            row.dataset.id, row.dataset.name, score, ccu, growth,
            row.dataset.alerts, row.dataset.recent,
            (row.dataset.flags || '').split(' ').join('|')
          ].map(csvCell).join(','));
        });
        var blob = new Blob(['\ufeff' + lines.join('\n')],
          {type: 'text/csv;charset=utf-8;'});
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = 'scout-games.csv';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
        var oldLabel = exportButton.textContent;
        exportButton.textContent = 'Exported ✓';
        setTimeout(function(){ exportButton.textContent = oldLabel; }, 1600);
      });
    }

    // "/" jumps to search, Escape clears it — the usual console habits.
    if (search) {
      document.addEventListener('keydown', function(event){
        var tag = (document.activeElement && document.activeElement.tagName) || '';
        if (event.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA' &&
            tag !== 'SELECT') {
          event.preventDefault();
          search.focus();
        }
        if (event.key === 'Escape' && document.activeElement === search) {
          search.value = '';
          apply();
        }
      });
    }
    apply();
  }

  // ---- watchlist: search, chips, sort ----
  var watch = document.querySelector('[data-watchlist]');
  if (watch) {
    var wrows = Array.prototype.slice.call(
      watch.querySelectorAll('tbody tr[data-watch]')
    );
    var wsearch = watch.querySelector('[data-wsearch]');
    var wchips = Array.prototype.slice.call(watch.querySelectorAll('[data-wfilter]'));
    var wsort = watch.querySelector('[data-wsort]');
    var wcount = watch.querySelector('[data-wcount]');
    var wno = watch.querySelector('[data-wno-results]');
    var wfilter = 'all';

    function wmatches(row, filter) {
      if (filter === 'all') return true;
      return (' ' + (row.dataset.flags || '') + ' ').indexOf(' ' + filter + ' ') !== -1;
    }
    function wapply() {
      var query = (wsearch && wsearch.value || '').toLowerCase().trim();
      var shown = 0;
      wrows.forEach(function(row){
        var haystack = ((row.dataset.name || '') + ' ' +
                        (row.dataset.id || '')).toLowerCase();
        var visible = (!query || haystack.indexOf(query) !== -1) &&
                      wmatches(row, wfilter);
        row.hidden = !visible;
        if (visible) shown += 1;
      });
      if (wcount) wcount.textContent = String(shown);
      if (wno) wno.hidden = shown !== 0 || wrows.length === 0;
    }
    if (wsearch) wsearch.addEventListener('input', wapply);
    wchips.forEach(function(chip){
      chip.addEventListener('click', function(){
        wchips.forEach(function(other){ other.classList.remove('active'); });
        chip.classList.add('active');
        wfilter = chip.dataset.wfilter || 'all';
        wapply();
      });
    });
    if (wsort) {
      wsort.addEventListener('change', function(){
        var key = wsort.value;
        var body = watch.querySelector('tbody');
        wrows.sort(function(a, b){
          if (key === 'name') {
            return (a.dataset.name || '').localeCompare(b.dataset.name || '');
          }
          if (key === 'recent') {
            return (b.dataset.recent || '').localeCompare(a.dataset.recent || '');
          }
          var av = Number(a.dataset[key]);
          var bv = Number(b.dataset[key]);
          if (!isFinite(av)) av = -1e15;
          if (!isFinite(bv)) bv = -1e15;
          return bv - av;
        });
        wrows.forEach(function(row){ body.appendChild(row); });
      });
    }
    if (wsearch) {
      document.addEventListener('keydown', function(event){
        var tag = (document.activeElement && document.activeElement.tagName) || '';
        if (event.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA' &&
            tag !== 'SELECT') {
          event.preventDefault();
          wsearch.focus();
        }
        if (event.key === 'Escape' && document.activeElement === wsearch) {
          wsearch.value = '';
          wapply();
        }
      });
    }
    wapply();
  }

  // ---- compare pickers: change a side, reload the comparison ----
  var compareForm = document.querySelector('[data-compare-form]');
  if (compareForm) {
    Array.prototype.slice.call(compareForm.querySelectorAll('select'))
      .forEach(function(select){
        select.addEventListener('change', function(){ compareForm.submit(); });
      });
  }

  // ---- game detail: chart range tabs ----
  Array.prototype.slice.call(document.querySelectorAll('[data-chart]')).forEach(
    function(wrap){
      var tabs = Array.prototype.slice.call(wrap.querySelectorAll('[data-range]'));
      var panes = Array.prototype.slice.call(wrap.querySelectorAll('[data-pane]'));
      tabs.forEach(function(tab){
        tab.addEventListener('click', function(){
          tabs.forEach(function(other){ other.setAttribute('aria-selected', 'false'); });
          tab.setAttribute('aria-selected', 'true');
          panes.forEach(function(pane){
            pane.hidden = pane.dataset.pane !== tab.dataset.range;
          });
        });
      });
    }
  );

  // ---- dashboard auto-refresh: keep the reader where they were ----
  try {
    var refreshMeta = document.querySelector('meta[http-equiv="refresh"]');
    if (refreshMeta) {
      var scrollKey = 'scout-scroll:' + location.pathname;
      var saved = sessionStorage.getItem(scrollKey);
      if (saved !== null) {
        requestAnimationFrame(function(){
          window.scrollTo(0, parseInt(saved, 10) || 0);
        });
      }
      var scrollTimer = null;
      window.addEventListener('scroll', function(){
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(function(){
          try { sessionStorage.setItem(scrollKey, String(window.scrollY)); }
          catch (err) {}
        }, 120);
      }, {passive: true});
    }
  } catch (err) {}
})();
"""


def _nav(active: str) -> str:
    links = (
        ("overview", "/dashboard", "Overview"),
        ("market", "/market", "Market"),
        ("games", "/games", "Games"),
        ("compare", "/compare", "Compare"),
        ("watchlist", "/watchlist", "Watchlist"),
    )
    rendered = []
    for key, href, label in links:
        current = ' aria-current="page"' if key == active else ""
        rendered.append(f'<a href="{href}"{current}>{label}</a>')
    return "".join(rendered)


def page(title: str, body: str, active: str = "overview",
         refresh_seconds: Optional[int] = None) -> str:
    """The shared shell: one inline stylesheet, one inline script block."""
    refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds else ""
    )
    generated = _now().strftime("%Y-%m-%d %H:%M UTC")
    record_current = ' aria-current="page"' if active == "record" else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#06090d">
<meta name="robots" content="noindex">
<title>{_esc(title)}</title>
{refresh}
<style>{_CSS}</style>
</head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="topbar"><div class="shell topbar-inner">
  <a class="brand" href="/dashboard" aria-label="Scout home">
    <span class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
        stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="8"></circle>
        <path d="M12 4v3M12 17v3M4 12h3M17 12h3"></path>
        <circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"></circle>
      </svg>
    </span>
    <span class="brand-text">Scout<small>Roblox intelligence</small></span>
  </a>
  <nav class="nav" aria-label="Primary">{_nav(active)}</nav>
  <div class="top-right">
    <span class="live"><span class="live-dot"></span>live</span>
    <a class="top-link" href="/scorecard"{record_current}>Record</a>
    <a class="top-link" href="/api/v1/leaderboard">API</a>
  </div>
</div></header>
<main id="main">{body}</main>
<footer class="footer"><div class="shell footer-inner">
  <span>Scout reads the bot's own snapshot store — no third-party calls,
  no external assets. All times UTC.</span>
  <span class="footer-links"><a href="/scorecard">Track record</a>
    <a href="/api/v1/leaderboard">API</a></span>
  <span class="mono">generated {generated}</span>
</div></footer>
<script>{_UI_JS}</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _signal_card(market, overview) -> str:
    rows = []
    for label, item, metric, note in (
        (
            "Top mover · 24h",
            market["movers"][0] if market["movers"] else None,
            (
                f'<span class="up">+{market["movers"][0]["growth_pct"]:.0f}%</span>'
                if market["movers"] else ""
            ),
            (
                f"{_fmt_int(market['movers'][0]['last_players'])} CCU"
                if market["movers"] else ""
            ),
        ),
        (
            "Best chart climb",
            market["climbers"][0] if market["climbers"] else None,
            (
                f'<span class="up">+{market["climbers"][0]["delta"]}</span>'
                if market["climbers"] else ""
            ),
            (
                f'#{market["climbers"][0]["position"]} on '
                f'{_esc(market["climbers"][0]["sort_label"] or "a chart")}'
                if market["climbers"] else ""
            ),
        ),
        (
            "Radar catch",
            market["ascents"][0] if market["ascents"] else None,
            (
                f'<span class="up">{_fmt_compact(market["ascents"][0]["ccu"])}</span>'
                if market["ascents"] else ""
            ),
            "CCU at catch" if market["ascents"] else "",
        ),
    ):
        if item is None:
            continue
        name = item.get("game_name") or f"Game {item.get('game_id')}"
        rows.append(
            f'<a class="signal-row" href="/game/{int(item["game_id"])}">'
            f'<span class="signal-label">{_esc(label)}</span>'
            f'<span class="signal-name">{_esc(name)}</span>'
            f'<span class="signal-metric">{metric}<small>{note}</small></span>'
            f"</a>"
        )
    if not rows:
        rows.append(
            '<div class="signal-row"><span class="signal-label">Signal</span>'
            '<span class="signal-name muted">Waiting for the next scan</span>'
            '<span class="signal-metric dim">—</span></div>'
        )
    last_scan = overview.get("last_scan") or overview.get("last_snapshot")
    return f"""
    <aside class="signal">
      <div class="signal-head">
        <span class="signal-title">Live board</span>
        <span class="badge mint">SCOUTING</span>
      </div>
      {''.join(rows)}
      <div class="signal-foot">
        <span>Last scan {_esc(_time_ago(last_scan))}</span>
        <span>{_fmt_int(overview.get('snapshots'))} snapshots</span>
      </div>
    </aside>"""


def _stat(label, value, foot) -> str:
    return (
        f'<div class="stat"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-foot">{foot}</div></div>'
    )


def _mover_list(movers, empty_text) -> str:
    if not movers:
        return _empty(empty_text)
    items = []
    for mover in movers:
        spark = sparkline_svg(mover.get("spark") or [], width=92, height=28)
        items.append(
            f'<li><div class="list-main">'
            f'<a class="list-name" href="/game/{int(mover["game_id"])}">'
            f'{_esc(mover["game_name"])}</a>'
            f'<span class="list-meta">{_fmt_int(mover["last_players"])} CCU · '
            f'{int(mover["snapshot_count"])} scans</span></div>'
            f'<span class="mover-spark">{spark or ""}</span>'
            f'<span class="list-side up">+{mover["growth_pct"]:.0f}%</span></li>'
        )
    return f'<ul class="list">{"".join(items)}</ul>'


def _climber_list(climbers) -> str:
    if not climbers:
        return _empty("No chart climbs recorded yet.",
                      "Positions are recorded once per scan cycle.")
    items = []
    for climber in climbers:
        label = climber.get("sort_label") or "Roblox chart"
        items.append(
            f'<li><div class="list-main">'
            f'<a class="list-name" href="/game/{int(climber["game_id"])}">'
            f'{_esc(climber["game_name"])}</a>'
            f'<span class="list-meta">{_esc(label)}</span></div>'
            f'<span class="list-side"><span class="muted">'
            f'#{int(climber["prev_position"])} → </span>'
            f'<span class="up">#{int(climber["position"])}</span></span></li>'
        )
    return f'<ul class="list">{"".join(items)}</ul>'


def _ascent_list(ascents) -> str:
    if not ascents:
        return _empty("Radar is quiet.", "No qualifying breakout in the window.")
    items = []
    for event in ascents:
        tone = "mint" if event["kind"] == "escalate" else "blue"
        label = "continues" if event["kind"] == "escalate" else "new"
        items.append(
            f'<li><div class="list-main">'
            f'<a class="list-name" href="/game/{int(event["game_id"])}">'
            f'{_esc(event["game_name"])}</a>'
            f'<span class="list-meta">{_esc(_time_ago(event["alert_ts"]))}</span>'
            f'</div><span class="list-side">{_badge(label, tone)} '
            f'<span class="muted">{_fmt_compact(event["ccu"])}</span></span></li>'
        )
    return f'<ul class="list">{"".join(items)}</ul>'


def _recent_alert_list(alerts) -> str:
    if not alerts:
        return _empty("No alerts recorded yet.",
                      "The board fills when the scanner posts its first match.")
    items = []
    for alert in alerts:
        gid = int(alert["game_id"])
        score = alert.get("best_scout_score")
        items.append(
            f'<li><div class="list-main">'
            f'<a class="list-name" href="/game/{gid}">'
            f'{_esc(alert.get("game_name") or f"Game {gid}")}</a>'
            f'<span class="list-meta">{_esc(_time_ago(alert.get("first_alerted")))}'
            f'</span></div>'
            f'<span class="list-side">{_score(score)}</span></li>'
        )
    return f'<ul class="list">{"".join(items)}</ul>'


def _opportunity_table(entries, series) -> str:
    if not entries:
        return _empty("No alerts recorded yet.",
                      "The board fills when the scanner posts its first match.")
    rows = []
    for rank, entry in enumerate(entries, 1):
        gid = int(entry["game_id"])
        points = _window(series.get(gid, []), 24 * 7)
        growth = (
            _pct_change(points[0]["players"], points[-1]["players"])
            if len(points) >= 2 else None
        )
        latest = points[-1]["players"] if points else None
        rows.append(
            f"<tr><td class='dim'>{rank}</td>"
            f"<td><a class='game-link' href='/game/{gid}'>"
            f"<span class='game-name'>{_esc(entry.get('game_name') or f'Game {gid}')}"
            f"</span><span class='game-id'>#{gid}</span></a></td>"
            f"<td>{_score(entry.get('best_scout_score'))}</td>"
            f"<td class='num'>{_pct_html(growth)}</td>"
            f"<td class='num'>{_fmt_int(latest)}</td>"
            f"<td class='num dim'>{int(entry.get('alert_count') or 1)}</td></tr>"
        )
    return (
        '<div class="scroll"><table class="table"><thead><tr>'
        "<th>#</th><th>Game</th><th>Scout score</th><th class='num'>7d</th>"
        "<th class='num'>CCU</th><th class='num'>Alerts</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _portfolio_cards(projects, series) -> str:
    if not projects:
        return _empty("No portfolio projects registered.",
                      "Add one with manage_portfolio.py to light up this shelf.")
    cards = []
    for project in projects:
        gid = int(project["game_id"])
        stats = project.get("stats") or {}
        growth = stats.get("growth") or {}
        name = project.get("name") or f"Game {gid}"
        initials = "".join(
            word[0] for word in str(name).split()[:2]
        ).upper() or str(gid)[:2]
        media = f'<span class="monogram">{_esc(initials)}</span>'
        values = [
            point["players"] for point in series.get(gid, [])
            if point.get("players") is not None
        ]
        spark = sparkline_svg(values, width=150, height=34)
        cards.append(
            f'<a class="project" href="/game/{gid}" '
            f'title="{_fmt_int(stats.get("players"))} CCU · {_esc(name)}">'
            f'<div class="project-top">{media}<div style="min-width:0">'
            f'<div class="project-name">{_esc(name)}</div>'
            f'<div class="project-role">{_esc(project.get("role") or "Project")}'
            f"</div></div></div>"
            f'<div class="project-stats"><div>'
            f'<div class="project-ccu">{_fmt_int(stats.get("players"))}'
            f" <small>CCU</small></div>"
            f'<div class="project-growth">24h {_pct_html(growth.get("players_24h"))}'
            f' · 7d {_pct_html(growth.get("players_7d"))}</div></div>'
            f"<div>{spark or ''}</div></div></a>"
        )
    return f'<div class="projects">{"".join(cards)}</div>'


def render_dashboard(portfolio: Optional[dict] = None) -> str:
    overview = _overview_data()
    market = _market_data()
    alerts = sorted(
        _alerted_rows(),
        key=lambda r: (
            -(r.get("best_scout_score") if isinstance(r.get("best_scout_score"), (int, float)) else -1),
            str(r.get("first_alerted") or ""),
        ),
    )[:8]
    series = _series_by_game(
        [int(a["game_id"]) for a in alerts if a.get("game_id") is not None],
        hours=24 * 7,
    )
    projects = (portfolio or {}).get("projects", [])
    project_series = {}
    if projects:
        try:
            project_series = portfolio_db.get_snapshots_bulk(
                [int(p["game_id"]) for p in projects], hours=24
            )
        except Exception as exc:
            logger.debug("portfolio sparkline read failed: %s", exc)

    tracked = overview["tracked"]
    stats = "".join(
        (
            _stat(
                "Games tracked",
                _fmt_int(tracked),
                f"{_fmt_int(overview['snapshots'])} snapshots · "
                f"{_fmt_int(overview['charted'])} on charts",
            ),
            _stat(
                "Alerts fired",
                _fmt_int(overview["alerts"]),
                f"+{_fmt_int(overview['alerts_7d'])} in the last 7 days",
            ),
            _stat(
                "Watchlist",
                _fmt_int(overview["watch_games"]),
                f"{_fmt_int(overview['human_watches'])} human · "
                f"{_fmt_int(overview['auto_watches'])} auto-tracked",
            ),
            _stat(
                "Last reading",
                _esc(_time_ago(overview.get("last_scan")
                               or overview.get("last_snapshot"))),
                f"{_fmt_int(overview['scans_24h'])} scans in 24h · "
                f"{_fmt_int(overview['radar_24h'])} radar catches",
            ),
        )
    )

    hero = f"""
    <section class="hero">
      <div>
        <p class="eyebrow">Roblox acquisition intelligence</p>
        <h1>Find the next breakout
          <span class="grad">before the charts do.</span></h1>
        <p class="lede">Scout watches CCU movement, chart climbs and early
          breakouts across the Roblox catalog — then grades its own calls.
          This is the live board, read straight from the bot's snapshot
          store.</p>
        <div class="hero-actions">
          <a class="btn btn-primary" href="/games">Explore the game index
            <span aria-hidden="true">→</span></a>
          <a class="btn btn-ghost" href="/scorecard">See the track record
            <span aria-hidden="true">→</span></a>
          <span class="hero-note">auto-refreshes every 5 min</span>
        </div>
      </div>
      {_signal_card(market, overview)}
    </section>
    <section class="stats" aria-label="Overview numbers">{stats}</section>
    """

    body = f"""
    <div class="shell">
      {hero}
      <div class="layout-2">
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Opportunity board</h2>
              <p class="panel-sub">Highest-scoring games the bot has alerted
                in the last 30 days</p></div>
            <a class="panel-link" href="/games">All games →</a>
          </div>
          <div class="panel-body">{_opportunity_table(alerts, series)}</div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Market pulse</h2>
              <p class="panel-sub">Biggest 24h CCU movers with a fresh read</p></div>
            <a class="panel-link" href="/market">Full market read →</a>
          </div>
          <div class="panel-body">
            {_mover_list(market["movers"],
                         "No fresh movement yet — snapshots build as scans run.")}
          </div>
        </section>
      </div>
      <div class="layout-3">
        <section class="panel">
          <div class="panel-head"><div><h2 class="panel-title">Chart climbers</h2>
            <p class="panel-sub">Cycle-over-cycle position gains</p></div></div>
          <div class="panel-body">{_climber_list(market["climbers"])}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><div><h2 class="panel-title">Ascent radar</h2>
            <p class="panel-sub">Breakouts caught between scan cycles</p></div></div>
          <div class="panel-body">{_ascent_list(market["ascents"])}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><div><h2 class="panel-title">Latest alerts</h2>
            <p class="panel-sub">Newest calls from the scanner</p></div></div>
          <div class="panel-body">{_recent_alert_list(market["recent_alerts"])}</div>
        </section>
      </div>
      <section class="panel" style="margin-top:16px">
        <div class="panel-head">
          <div><h2 class="panel-title">Portfolio</h2>
            <p class="panel-sub">Public projects with live numbers</p></div>
          <a class="panel-link" href="/api/v1/portfolio">JSON →</a>
        </div>
        {_portfolio_cards(projects, project_series)}
      </section>
    </div>"""
    return page("Scout — live market board", body, active="overview",
                refresh_seconds=300)


# ---------------------------------------------------------------------------
# Games index
# ---------------------------------------------------------------------------

def render_games() -> str:
    index = _game_index()
    rows = index["rows"]
    total = index["total"]
    history_games = sum(1 for r in rows if r["spark"])
    charted = sum(1 for r in rows if r["chart_best"] is not None)
    main = []
    for row in rows:
        gid = row["game_id"]
        spark = sparkline_svg(row["spark"], width=120, height=32)
        trend = spark or '<span class="dim">no history yet</span>'
        score = row["score"]
        recent = row["last_seen"] or ""
        main.append(
            f"<tr data-game data-id=\"{gid}\" "
            f"data-name=\"{_esc(row['name'])}\" "
            f"data-score=\"{score if isinstance(score, (int, float)) else -1}\" "
            f"data-ccu=\"{row['players'] if isinstance(row['players'], (int, float)) else -1}\" "
            f"data-growth=\"{row['growth'] if row['growth'] is not None else -1e9}\" "
            f"data-alerts=\"{row['alerts']}\" "
            f"data-recent=\"{_esc(recent)}\" "
            f"data-flags=\"{' '.join(row['flags'])}\">"
            f"<td><a class='game-link' href='/game/{gid}'>"
            f"<span class='game-name'>{_esc(row['name'])}</span>"
            f"<span class='game-id'>#{gid}</span></a></td>"
            f"<td class='hide-sm'>{_flag_badges(row)}</td>"
            f"<td class='hide-sm'>{trend}</td>"
            f"<td class='num'>{_fmt_int(row['players'])}</td>"
            f"<td class='num'>{_pct_html(row['growth'])}</td>"
            f"<td>{_score(score)}</td>"
            f"<td class='num dim'>{row['alerts']}</td>"
            f"<td class='num dim hide-sm'>{_esc(_time_ago(recent))}</td></tr>"
        )
    table = f"""
    <div class="scroll">
      <table class="table" id="games-table">
        <thead><tr>
          <th>Game</th><th class="hide-sm">Signals</th>
          <th class="hide-sm">7d trend</th><th class="num">CCU</th>
          <th class="num">7d</th><th>Score</th><th class="num">Alerts</th>
          <th class="num hide-sm">Last seen</th>
        </tr></thead>
        <tbody>
          {''.join(main) if main else (
            '<tr><td colspan="8">' + _empty(
                "No alerts recorded yet.",
                "Run /scan or add a portfolio project — this board fills itself."
            ) + "</td></tr>"
          )}
          <tr data-no-results hidden><td colspan="8">
            {_empty("No match.", "Try a different name, id or signal filter.")}
          </td></tr>
        </tbody>
      </table>
    </div>"""

    body = f"""
    <div class="shell">
      <header class="page-head">
        <p class="eyebrow">The game index</p>
        <h1>Every game Scout knows.</h1>
        <p class="lede">Alerts, watches and portfolio projects in one
          searchable board. Growth is measured from the bot's own snapshots,
          never a third-party estimate.</p>
      </header>
      <section class="panel" data-index>
        <div class="toolbar">
          <label class="search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7"></circle>
              <path d="m20 20-3.5-3.5"></path>
            </svg>
            <input data-search type="search"
              placeholder="Search by name or game id…" aria-label="Search games">
          </label>
          <div class="chips" role="group" aria-label="Filter games">
            <button class="chip active" data-filter="all" type="button">All</button>
            <button class="chip" data-filter="alerted" type="button">Alerted</button>
            <button class="chip" data-filter="watched" type="button">Watched</button>
            <button class="chip" data-filter="charts" type="button">On charts</button>
            <button class="chip" data-filter="rising" type="button">Rising</button>
            <button class="chip" data-filter="falling" type="button">Falling</button>
            <button class="chip" data-filter="ai" type="button">AI verdict</button>
            <button class="chip" data-filter="portfolio" type="button">Portfolio</button>
          </div>
          <select data-sort aria-label="Sort games">
            <option value="score">Best score</option>
            <option value="ccu">Most CCU</option>
            <option value="growth">Fastest growth</option>
            <option value="alerts">Most alerts</option>
            <option value="recent">Recently seen</option>
            <option value="name">Name A–Z</option>
          </select>
          <button class="chip export" type="button" data-export
            title="Export the visible rows as CSV">Export CSV</button>
          <span class="toolbar-note"><b data-count>{total}</b> of {total} games</span>
        </div>
        {table}
      </section>
      <p class="footnote">{history_games} games have snapshot history ·
        {charted} are on a Roblox chart · search, sort and CSV export run
        locally in your browser · press <b>/</b> to search.</p>
    </div>"""
    return page("Scout — game index", body, active="games")


# ---------------------------------------------------------------------------
# Watchlist / tracking desk
# ---------------------------------------------------------------------------

def _watch_status_badge(status: str) -> str:
    tone = {
        "Spiking": "mint",
        "Rising": "mint",
        "Cooling": "rose",
        "Stale": "amber",
    }.get(status, "dim")
    return _badge(status, tone)


def render_watchlist() -> str:
    data = _watchlist_data()
    rows = data["rows"]
    total = len(rows)
    strongest = data["strongest"]
    strongest_value = (
        f'+{strongest["growth_24h"]:.0f}%'
        if strongest and strongest["growth_24h"] is not None else "—"
    )
    stats = "".join(
        (
            _stat("Tracked games", _fmt_int(total),
                  f'{_fmt_int(data["human"])} with a human watch · '
                  f'{_fmt_int(data["auto"])} auto'),
            _stat("Moving up", _fmt_int(data["moving"]),
                  "spiking or rising on the 24h / 7d read"),
            _stat("Stale reads", _fmt_int(data["stale"]),
                  "no tracker update in the last 6 hours"),
            _stat("Strongest 24h", strongest_value,
                  _esc(strongest["name"]) if strongest else "no fresh move yet"),
        )
    )

    table_rows = []
    for row in rows:
        gid = row["game_id"]
        spark = sparkline_svg(row["spark"], width=120, height=32)
        spark_html = spark or '<span class="dim">no history yet</span>'
        tags = []
        if row["human"]:
            tags.append(_badge("Mine", "blue"))
        if row["auto"]:
            tags.append(_badge("Auto", "dim"))
        meta_tags = "".join(tags)
        recent = row["last_checked"] or ""
        momentum = row["momentum"] if row["momentum"] is not None else -1e9
        growth7 = row["growth_7d"] if row["growth_7d"] is not None else -1e9
        table_rows.append(
            f"<tr data-watch data-id=\"{gid}\" "
            f"data-name=\"{_esc(row['name'])}\" "
            f"data-ccu=\"{row['players'] if isinstance(row['players'], (int, float)) else -1}\" "
            f"data-momentum=\"{momentum}\" data-growth=\"{growth7}\" "
            f"data-recent=\"{_esc(recent)}\" "
            f"data-flags=\"{' '.join(row['flags'])}\">"
            f"<td><a class='game-link' href='/game/{gid}'>"
            f"<span class='game-name'>{_esc(row['name'])}</span>"
            f"<span class='game-id'>#{gid} {meta_tags}</span></a></td>"
            f"<td class='hide-sm'>{spark_html}</td>"
            f"<td class='num'>{_fmt_int(row['players'])}</td>"
            f"<td class='num'>{_pct_html(row['growth_24h'])}</td>"
            f"<td class='num hide-xs'>{_pct_html(row['growth_7d'])}</td>"
            f"<td class='num hide-sm'>{_pct_html(row['since_watch'])}</td>"
            f"<td>{_watch_status_badge(row['status'])}</td>"
            f"<td class='num dim hide-sm'>{_esc(_time_ago(recent))}</td></tr>"
        )

    table = f"""
    <div class="scroll">
      <table class="table" id="watch-table">
        <thead><tr>
          <th>Game</th><th class="hide-sm">14d trend</th>
          <th class="num">CCU</th><th class="num">24h</th>
          <th class="num hide-xs">7d</th><th class="num hide-sm">Since watch</th>
          <th>Status</th><th class="num hide-sm">Last check</th>
        </tr></thead>
        <tbody>
          {''.join(table_rows) if table_rows else (
            '<tr><td colspan="8">' + _empty(
                "No watched games yet.",
                "Use /watch <game_id> in Discord, or Accept an alert to start tracking it."
            ) + "</td></tr>"
          )}
          <tr data-wno-results hidden><td colspan="8">
            {_empty("No match.", "Try a different name, id or tracking status.")}
          </td></tr>
        </tbody>
      </table>
    </div>"""

    body = f"""
    <div class="shell">
      <header class="page-head">
        <p class="eyebrow">Tracking desk</p>
        <h1>What are you watching?</h1>
        <p class="lede">Every game the bot is tracking, with the freshness of
          its latest read and the move since it was added. Staleness is shown
          honestly — an old reading is never presented as a live signal.</p>
      </header>
      <section class="stats" aria-label="Watchlist numbers">{stats}</section>
      <section class="panel" data-watchlist>
        <div class="toolbar">
          <label class="search">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7"></circle>
              <path d="m20 20-3.5-3.5"></path>
            </svg>
            <input data-wsearch type="search"
              placeholder="Search watched games…" aria-label="Search watched games">
          </label>
          <div class="chips" role="group" aria-label="Filter watched games">
            <button class="chip active" data-wfilter="all" type="button">All</button>
            <button class="chip" data-wfilter="mine" type="button">Mine</button>
            <button class="chip" data-wfilter="auto" type="button">Auto</button>
            <button class="chip" data-wfilter="moving" type="button">Moving</button>
            <button class="chip" data-wfilter="cooling" type="button">Cooling</button>
            <button class="chip" data-wfilter="stale" type="button">Stale</button>
          </div>
          <select data-wsort aria-label="Sort watched games">
            <option value="momentum">Most momentum</option>
            <option value="growth">7d growth</option>
            <option value="ccu">Most CCU</option>
            <option value="recent">Recently checked</option>
            <option value="name">Name A–Z</option>
          </select>
          <span class="toolbar-note"><b data-wcount>{total}</b> of {total} tracked</span>
        </div>
        {table}
      </section>
      <p class="footnote">{data["stale"]} stale reads · since-watch growth uses
        the oldest snapshot inside the 30-day store · times are UTC ·
        <a href="/scorecard">How do the calls grade? Track record →</a></p>
    </div>"""
    return page("Scout — watchlist", body, active="watchlist")


# ---------------------------------------------------------------------------
# Market weather
# ---------------------------------------------------------------------------

# A reading older than this is not evidence of where the market is *now*.
# The same six-hour bar the watchlist uses for its Stale badge.
_MARKET_FRESH_HOURS = 6
_MARKET_DRAWDOWN_FLOOR = -10.0

# Fixed buckets, so one day's histogram reads the same as the next.
# (low, high, short label, tone): low inclusive, high exclusive.
_MARKET_HIST_BUCKETS = (
    (None, -50.0, "≤−50", "rose"),
    (-50.0, -25.0, "−50", "rose"),
    (-25.0, -10.0, "−25", "rose"),
    (-10.0, 0.0, "−10", "amber"),
    (0.0, 10.0, "0", "mint"),
    (10.0, 25.0, "+10", "mint"),
    (25.0, 50.0, "+25", "mint"),
    (50.0, None, "≥+50", "mint"),
)


def _market_name_map() -> dict:
    """Names and flags for every game the bot has a record of."""
    names: dict[int, str] = {}
    alerted, watched, portfolio = set(), set(), set()

    def note(row, key, bucket=None, overwrite=False):
        gid = _as_int(row.get("game_id"))
        if gid is None:
            return
        if bucket is not None:
            bucket.add(gid)
        name = (row.get(key) or "").strip()
        if not name:
            return
        if overwrite:
            names[gid] = name
        else:
            names.setdefault(gid, name)

    for row in _portfolio_rows():
        note(row, "display_name", portfolio)
    for row in _watched_rows():
        note(row, "game_name", watched)
    for row in _radar_map().values():
        note(row, "game_name")
    for row in _alerted_rows():
        # The alerted ledger is the bot's own naming of a call, so it wins.
        note(row, "game_name", alerted, overwrite=True)
    return {
        "names": names,
        "alerted": alerted,
        "watched": watched,
        "portfolio": portfolio,
    }


def _market_rows() -> list[dict]:
    """One row per game in the 30-day snapshot store, with fresh flags.

    Moves are first-versus-last inside each window, the same rule the
    movers selector uses. A game whose newest reading is older than six
    hours is kept for context but excluded from breadth and leaders — an
    old move is not the current tape.
    """
    ids = []
    for row in _rows(
        "SELECT DISTINCT game_id FROM game_history "
        "WHERE timestamp >= datetime('now','-30 days')"
    ):
        gid = _as_int(row.get("game_id"))
        if gid is not None:
            ids.append(gid)

    series = _series_by_game(ids, hours=24 * 30)
    meta = _market_name_map()
    fresh_cutoff = _now() - timedelta(hours=_MARKET_FRESH_HOURS)
    rows = []

    for gid in sorted(series):
        points = [
            p for p in series[gid]
            if p.get("players") is not None and p.get("dt") is not None
        ]
        if not points:
            continue
        latest = points[-1]
        day = _window(points, 24)
        week = _window(points, 24 * 7)
        peak = max(float(p["players"]) for p in points)
        latest_players = latest["players"]
        off_peak = None
        if peak > 0 and isinstance(latest_players, (int, float)):
            off_peak = round((float(latest_players) - peak) / peak * 100.0, 1)

        rows.append({
            "game_id": gid,
            "name": meta["names"].get(gid) or f"Game {gid}",
            "players": latest_players,
            "last_seen": latest["timestamp"],
            "fresh": latest["dt"] >= fresh_cutoff,
            "move_24h": (
                _pct_change(day[0]["players"], day[-1]["players"])
                if len(day) >= 2 else None
            ),
            "move_7d": (
                _pct_change(week[0]["players"], week[-1]["players"])
                if len(week) >= 2 else None
            ),
            "peak_30d": peak,
            "off_peak": off_peak,
            "alerted": gid in meta["alerted"],
            "watched": gid in meta["watched"],
            "portfolio": gid in meta["portfolio"],
        })
    return rows


def _market_breadth(rows, key: str) -> dict:
    """Advancers, decliners and the median move over one window."""
    moves = [
        float(r[key]) for r in rows
        if r.get("fresh") and isinstance(r.get(key), (int, float))
    ]
    graded = len(moves)
    up = sum(1 for m in moves if m > 0)
    down = sum(1 for m in moves if m < 0)

    def share(count):
        return round(count / graded * 100.0, 1) if graded else 0.0

    return {
        "graded": graded,
        "up": up,
        "down": down,
        "flat": graded - up - down,
        "up_pct": share(up),
        "down_pct": share(down),
        "flat_pct": share(graded - up - down),
        "median": _record_median(moves),
    }


def _market_verdict(breadth: dict) -> dict:
    """Turn breadth into a plain-language market state.

    Honest by construction: fewer than three fresh pairs is never a
    verdict, it is a thin tape. Thresholds are fixed, not tuned to the
    sample, so the label cannot flatter a bad day.
    """
    graded = breadth.get("graded") or 0
    median = breadth.get("median")
    if graded < 3 or median is None:
        return {
            "label": "Thin tape",
            "tone": "dim",
            "read": "Too few games have two fresh readings to call a market.",
        }
    up = breadth["up_pct"]
    if up >= 60 and median >= 3:
        return {
            "label": "Risk-on",
            "tone": "mint",
            "read": "Breadth is broad and the median is positive.",
        }
    if up >= 55:
        return {
            "label": "Leaning up",
            "tone": "blue",
            "read": "More games are rising than falling, but not decisively.",
        }
    if up <= 40 and median <= -3:
        return {
            "label": "Risk-off",
            "tone": "rose",
            "read": "Breadth is negative and the median move is down.",
        }
    if up <= 45:
        return {
            "label": "Cooling",
            "tone": "amber",
            "read": "Slightly more games are falling than rising.",
        }
    return {
        "label": "Two-way tape",
        "tone": "blue",
        "read": "Advancers and decliners are balanced — a stock-picker's tape.",
    }


def _market_histogram(moves) -> list[dict]:
    """Count 24h moves into the fixed buckets (low inclusive, high exclusive)."""
    clean = [float(m) for m in moves if isinstance(m, (int, float))]
    buckets = []
    for low, high, label, tone in _MARKET_HIST_BUCKETS:
        count = 0
        for move in clean:
            if low is None and move < high:
                count += 1
            elif high is None and move >= low:
                count += 1
            elif low is not None and high is not None and low <= move < high:
                count += 1
        if low is None:
            span = f"below {high:g}%"
        elif high is None:
            span = f"{low:g}% or more"
        else:
            span = f"{low:g}% to {high:g}%"
        buckets.append({
            "label": label,
            "range": span.replace("-", "−"),
            "count": count,
            "tone": tone,
        })
    return buckets


def _spread_html(value) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    if value > 0:
        return f'<span class="up">+{value:.1f} pp</span>'
    if value < 0:
        return f'<span class="down">{value:.1f} pp</span>'
    return '<span class="flat">0.0 pp</span>'


def _market_breadth_html(label: str, breadth: dict) -> str:
    if not breadth["graded"]:
        return (
            f'<div class="breadth"><div class="breadth-top">'
            f'<span>{_esc(label)}</span>'
            f'<span class="dim">no fresh pair</span></div>'
            f'<div class="breadth-track"></div></div>'
        )
    parts = []
    for cls, share in (
        ("b-up", breadth["up_pct"]),
        ("b-flat", breadth["flat_pct"]),
        ("b-down", breadth["down_pct"]),
    ):
        if share > 0:
            parts.append(f'<i class="{cls}" style="width:{share:.1f}%"></i>')
    return (
        f'<div class="breadth">'
        f'<div class="breadth-top"><span>{_esc(label)}</span>'
        f'<span><b>{breadth["up_pct"]:.0f}%</b> up · '
        f'<b>{breadth["down_pct"]:.0f}%</b> down</span></div>'
        f'<div class="breadth-track" role="img" aria-label="'
        f'{_esc(label)} breadth: {breadth["up"]} up, {breadth["flat"]} flat, '
        f'{breadth["down"]} down">{''.join(parts)}</div>'
        f'<div class="breadth-scale"><span>{breadth["up"]} up · '
        f'{breadth["flat"]} flat</span><span>{breadth["down"]} down</span></div>'
        f'</div>'
    )


def _market_histogram_html(buckets: list[dict], graded: int) -> str:
    if not graded:
        return _empty(
            "No moves to chart yet.",
            "Two fresh readings per game is all this needs."
        )
    top = max((b["count"] for b in buckets), default=0) or 1
    cols = []
    for bucket in buckets:
        height = round(bucket["count"] / top * 100.0, 1)
        cols.append(
            f'<div class="hist-col" title="24h CCU move {_esc(bucket["range"])} '
            f'· {bucket["count"]} games">'
            f'<span class="hist-count">{bucket["count"] or ""}</span>'
            f'<div class="hist-track"><span class="hist-bar {bucket["tone"]}" '
            f'style="height:{max(height, 2) if bucket["count"] else 0}%"></span>'
            f'</div><span class="hist-label">{_esc(bucket["label"])}</span></div>'
        )
    return (
        f'<div class="hist" role="list" aria-label="Distribution of 24h '
        f'moves across {graded} games">{''.join(cols)}</div>'
    )


def _market_move_table(rows, key: str, descending=True, limit=5, empty="",
                       sign=0) -> str:
    """A compact leaders/laggards table.

    `sign` keeps a leaderboard honest: without it a market with fewer than
    five gainers would list a falling game under "biggest gains".
    """
    usable = [r for r in rows if isinstance(r.get(key), (int, float))]
    if sign > 0:
        usable = [r for r in usable if r[key] > 0]
    elif sign < 0:
        usable = [r for r in usable if r[key] < 0]
    usable.sort(key=lambda r: r[key], reverse=descending)
    if not usable:
        return f'<p class="market-empty">{_esc(empty)}</p>'
    body = []
    for row in usable[:limit]:
        body.append(
            f'<tr><td><a class="game-link" href="/game/{row["game_id"]}">'
            f'<span class="game-name">{_esc(row["name"])}</span>'
            f'<span class="game-id">#{row["game_id"]}</span></a></td>'
            f'<td class="num">{_fmt_int(row["players"])}</td>'
            f'<td class="num">{_pct_html(row[key])}</td></tr>'
        )
    return (
        '<div class="scroll"><table class="table market-table">'
        '<thead><tr><th>Game</th><th class="num">CCU</th>'
        '<th class="num">Move</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _market_drawdown_table(rows) -> str:
    thin = sorted(
        (
            r for r in rows
            if r.get("fresh") and isinstance(r.get("off_peak"), (int, float))
            and r["off_peak"] <= _MARKET_DRAWDOWN_FLOOR
        ),
        key=lambda r: r["off_peak"],
    )[:6]
    if not thin:
        return (
            '<p class="market-empty">No fresh game is more than 10% below '
            'its 30-day peak.</p>'
        )
    body = []
    for row in thin:
        body.append(
            f'<tr><td><a class="game-link" href="/game/{row["game_id"]}">'
            f'<span class="game-name">{_esc(row["name"])}</span>'
            f'<span class="game-id">#{row["game_id"]}</span></a></td>'
            f'<td class="num">{_fmt_int(row["players"])}</td>'
            f'<td class="num hide-sm">{_fmt_int(row["peak_30d"])}</td>'
            f'<td class="num">{_pct_html(row["off_peak"])}</td>'
            f'<td class="num hide-sm">{_pct_html(row["move_24h"])}</td></tr>'
        )
    return (
        '<div class="scroll"><table class="table market-table">'
        '<thead><tr><th>Game</th><th class="num">Now</th>'
        '<th class="num hide-sm">30d peak</th><th class="num">Off peak</th>'
        '<th class="num hide-sm">24h</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _market_book_table(portfolio_rows, rows, median_24h) -> str:
    if not portfolio_rows:
        return (
            '<p class="market-empty">No portfolio projects registered. '
            'The dashboard shows how to add them.</p>'
        )
    by_id = {r["game_id"]: r for r in rows}
    body = []
    for project in portfolio_rows:
        gid = _as_int(project.get("game_id"))
        entry = by_id.get(gid) or {}
        move = entry.get("move_24h")
        spread = (
            round(move - median_24h, 1)
            if isinstance(move, (int, float)) and median_24h is not None
            else None
        )
        body.append(
            f'<tr><td><a class="game-link" href="/game/{gid}">'
            f'<span class="game-name">{_esc(project.get("display_name"))}</span>'
            f'<span class="game-id">#{gid}</span></a></td>'
            f'<td class="num">{_fmt_int(entry.get("players"))}</td>'
            f'<td class="num">{_pct_html(move)}</td>'
            f'<td class="num hide-sm">{_pct_html(entry.get("move_7d"))}</td>'
            f'<td class="num">{_spread_html(spread)}</td></tr>'
        )
    return (
        '<div class="scroll"><table class="table market-table">'
        '<thead><tr><th>Project</th><th class="num">CCU</th>'
        '<th class="num">24h</th><th class="num hide-sm">7d</th>'
        '<th class="num">vs tape</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _chart_footprint() -> list[dict]:
    """Where the bot's charted games sit, per chart, over the last 48h."""
    return _rows(
        """
        SELECT COALESCE(NULLIF(sort_label, ''), sort_id) AS label,
               COUNT(DISTINCT game_id) AS games,
               MIN(position) AS best,
               MAX(recorded_at) AS latest
        FROM chart_positions
        WHERE recorded_at >= datetime('now','-2 days')
        GROUP BY COALESCE(NULLIF(sort_label, ''), sort_id)
        ORDER BY games DESC, label ASC
        LIMIT 8
        """
    )


def render_market() -> str:
    rows = _market_rows()
    fresh = [r for r in rows if r["fresh"]]
    b24 = _market_breadth(fresh, "move_24h")
    b7 = _market_breadth(fresh, "move_7d")
    verdict = _market_verdict(b24)
    median = b24["median"]

    if b24["graded"] >= 3 and median is not None:
        headline = (
            f'{b24["up"]} of {b24["graded"]} tracked games are up over '
            f'24 hours, and the median move is {median:+.1f}%. '
            f'{verdict["read"]}'
        )
    else:
        headline = (
            "The board has more history than it has fresh movement right "
            "now. Breadth needs at least two readings per game inside the "
            "window, so this fills in as scans run."
        )

    tracked_ccu = sum(
        float(r["players"]) for r in fresh
        if isinstance(r["players"], (int, float))
    )
    highs = sum(
        1 for r in fresh
        if isinstance(r.get("off_peak"), (int, float)) and r["off_peak"] >= -1.0
    )
    median_off_peak = _record_median(
        r["off_peak"] for r in fresh
        if isinstance(r.get("off_peak"), (int, float))
    )
    stats = "".join(
        (
            _stat(
                "Fresh reads",
                _fmt_int(len(fresh)),
                f"of {_fmt_int(len(rows))} games with 30-day history",
            ),
            _stat(
                "Tracked CCU",
                _fmt_compact(tracked_ccu),
                "latest reading across fresh games",
            ),
            _stat(
                "At 30-day highs",
                _fmt_int(highs),
                "within 1% of their own peak",
            ),
            _stat(
                "Median off peak",
                _pct_html(median_off_peak),
                "fresh games vs their 30d peak",
            ),
        )
    )

    moves = [
        r["move_24h"] for r in fresh
        if isinstance(r.get("move_24h"), (int, float))
    ]
    histogram = _market_histogram(moves)

    footprint = _chart_footprint()
    footprint_html = "".join(
        f'<div class="kv"><span class="kv-k">{_esc(row.get("label") or "Chart")}'
        f'</span><span class="kv-v">{_fmt_int(row.get("games"))} games · '
        f'best #{_fmt_int(row.get("best"))}</span></div>'
        for row in footprint
    ) or (
        '<p class="market-empty">No chart positions recorded in the last '
        '48 hours. The board fills when the scanner reads Roblox Charts.</p>'
    )
    last_chart = max(
        (str(row.get("latest") or "") for row in footprint), default=""
    )

    median_html = (
        f'{median:+.1f}%' if median is not None else "—"
    )
    body = f"""
    <div class="shell">
      <div class="market-hero">
        <section class="market-copy">
          <p class="eyebrow">Market weather</p>
          <h1>Read the whole tape.</h1>
          <p class="lede">{_esc(headline)}</p>
          <div class="market-call">
            <span class="market-median {verdict['tone']}">{_esc(median_html)}</span>
            <div>
              {_badge(verdict["label"], verdict["tone"])}
              <div class="market-call-note">median 24h move ·
                {_fmt_int(b24["graded"])} fresh games graded</div>
            </div>
          </div>
        </section>
        <section class="panel breadth-panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Breadth</h2>
              <p class="panel-sub">How many games are moving with the tape</p></div>
          </div>
          <div class="panel-body">
            {_market_breadth_html("Last 24 hours", b24)}
            {_market_breadth_html("Last 7 days", b7)}
            <div class="breadth-legend">
              <span><i class="b-up"></i>up</span>
              <span><i class="b-flat"></i>flat</span>
              <span><i class="b-down"></i>down</span>
            </div>
          </div>
        </section>
      </div>
      <section class="stats" aria-label="Market numbers">{stats}</section>
      <div class="layout-2">
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Move distribution</h2>
              <p class="panel-sub">Every fresh 24h move, in fixed buckets</p></div>
            <span class="panel-link">{_fmt_int(len(moves))} games</span>
          </div>
          <div class="panel-body">
            {_market_histogram_html(histogram, len(moves))}
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Chart footprint</h2>
              <p class="panel-sub">Where Scout's games sit on Roblox charts</p></div>
          </div>
          <div class="panel-body">
            {footprint_html}
            {f'<p class="market-foot">Last chart read {_esc(_time_ago(last_chart))}</p>' if footprint else ''}
          </div>
        </section>
      </div>
      <div class="layout-2">
        <section class="panel" data-market-movers="up">
          <div class="panel-head">
            <div><h2 class="panel-title">Leaders</h2>
              <p class="panel-sub">Biggest 24h gains with a fresh read</p></div>
            <a class="panel-link" href="/games">All games →</a>
          </div>
          <div class="panel-body">
            {_market_move_table(fresh, "move_24h", True, 5,
                                "No fresh gainers right now.", sign=1)}
          </div>
        </section>
        <section class="panel" data-market-movers="down">
          <div class="panel-head">
            <div><h2 class="panel-title">Laggards</h2>
              <p class="panel-sub">Biggest 24h declines with a fresh read</p></div>
          </div>
          <div class="panel-body">
            {_market_move_table(fresh, "move_24h", False, 5,
                                "No fresh decliners right now.", sign=-1)}
          </div>
        </section>
      </div>
      <div class="layout-2">
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Where the air is thin</h2>
              <p class="panel-sub">Furthest below their own 30-day peak</p></div>
          </div>
          <div class="panel-body">{_market_drawdown_table(fresh)}</div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Your book vs the tape</h2>
              <p class="panel-sub">Portfolio projects against the median move</p></div>
          </div>
          <div class="panel-body">
            {_market_book_table(_portfolio_rows(), fresh, median)}
          </div>
        </section>
      </div>
      <p class="footnote">Breadth compares the first and last reading inside
        each window for games with at least two readings and a read newer
        than six hours. Stale games are left out rather than counted at an
        old price. Drawdown is the latest reading against the game's own
        30-day peak. No game is weighted by size.
        <a href="/scorecard">Do the calls hold up? Track record →</a></p>
    </div>"""
    return page("Scout — market weather", body, active="market")


# ---------------------------------------------------------------------------
# Track record page
# ---------------------------------------------------------------------------

def _record_scatter(data: dict) -> str:
    points = [c for c in data["graded"] if c.get("score") is not None]
    if not points:
        return _empty(
            "Nothing to plot yet.",
            "Scored calls appear here once they reach the 72-hour mark."
        )

    width, height = 720, 300
    left, right, top, bottom = 56, 18, 18, 42
    plot_w = width - left - right
    plot_h = height - top - bottom
    scores = [c["score"] for c in points]
    x_min = max(0, min(scores) - 5)
    x_max = 100
    if x_max <= x_min:
        x_max = x_min + 10
    max_abs = max(20.0, max(abs(c["delta"]) for c in points))
    y_max = math.ceil(max_abs / 10.0) * 10

    def sx(score):
        return left + (score - x_min) / (x_max - x_min) * plot_w

    def sy(delta):
        return top + (y_max - delta) / (y_max * 2) * plot_h

    parts = [
        f'<svg class="record-scatter" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="72-hour CCU move against Scout score for '
        f'{len(points)} graded calls">'
    ]
    for tick in (y_max, 0, -y_max):
        y = sy(tick)
        parts.append(
            f'<line class="axis" x1="{left}" x2="{width - right}" '
            f'y1="{y:.1f}" y2="{y:.1f}"></line>'
        )
        label = f"{tick:+.0f}%" if tick else "0%"
        parts.append(
            f'<text class="axis-label" x="{left - 9}" y="{y + 3:.1f}" '
            f'text-anchor="end">{label}</text>'
        )
    for tick in (x_min, (x_min + x_max) // 2, x_max):
        x = sx(tick)
        parts.append(
            f'<line class="axis" x1="{x:.1f}" x2="{x:.1f}" '
            f'y1="{top}" y2="{height - bottom}"></line>'
        )
        parts.append(
            f'<text class="axis-label" x="{x:.1f}" '
            f'y="{height - bottom + 15}" text-anchor="middle">{tick}</text>'
        )
    parts.append(
        f'<line class="zero" x1="{left}" x2="{width - right}" '
        f'y1="{sy(0):.1f}" y2="{sy(0):.1f}"></line>'
    )
    for call in points:
        # A tiny deterministic offset keeps calls with similar scores/moves
        # from hiding each other; it is visual only, never used in grading.
        jx = ((call["game_id"] % 7) - 3) * 0.9
        jy = ((call["game_id"] % 5) - 2) * 0.8
        x = max(left, min(width - right, sx(call["score"]) + jx))
        y = max(top, min(height - bottom, sy(call["delta"]) + jy))
        tooltip = (
            f'{call["name"]} — score {call["score"]}, '
            f'{call["delta"]:+.1f}% at 72h'
        )
        parts.append(
            f'<circle class="dot {call["status"]}" cx="{x:.1f}" '
            f'cy="{y:.1f}" r="5.2">'
            f'<title>{_esc(tooltip)}</title></circle>'
        )
    parts.append(
        f'<text class="axis-label" x="{left + plot_w / 2:.1f}" '
        f'y="{height - 8}" text-anchor="middle">Scout score</text>'
    )
    parts.append(
        f'<text class="axis-label" x="14" y="{top + plot_h / 2:.1f}" '
        f'text-anchor="middle" '
        f'transform="rotate(-90 14 {top + plot_h / 2:.1f})">72h CCU move</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _record_distribution(data: dict) -> str:
    graded = data["graded"]
    if not graded:
        return _empty(
            "No graded calls yet.",
            "Each alert is graded 72 hours after it fires."
        )
    total = len(graded)
    segments = (
        ("win", "wins", "win", data["wins"]),
        ("quiet", "quiet", "quiet", data["quiet"]),
        ("miss", "misses", "miss", data["misses"]),
    )
    def word(label, singular, count):
        return singular if count == 1 else label

    bar = "".join(
        f'<span class="record-seg {tone}" style="width:{count / total * 100:.1f}%" '
        f'title="{count} {word(label, singular, count)}"></span>'
        for tone, label, singular, count in segments if count
    )
    legend = "".join(
        f'<span class="record-key"><i class="record-swatch {tone}"></i>'
        f'<b>{count}</b> {word(label, singular, count)}'
        f'<span class="dim">{count / total * 100:.0f}%</span></span>'
        for tone, label, singular, count in segments if count
    )
    return (
        f'<div class="record-bar" role="img" aria-label="Outcome spread of '
        f'{total} graded calls">{bar}</div>'
        f'<div class="record-legend">{legend}</div>'
    )


def _record_bands(data: dict) -> str:
    if not data["graded"]:
        return _empty(
            "No score bands to compare yet.",
            "Bands fill as calls reach their 72-hour mark."
        )
    rows = []
    for band in data["bands"]:
        if band["hit_rate"] is None:
            rate = '<span class="dim">—</span>'
            meter = '<div class="band-meter"><i style="width:0%"></i></div>'
        else:
            rate = f'{band["hit_rate"]:.0f}%'
            meter = (
                f'<div class="band-meter"><i style="width:{band["hit_rate"]:.1f}%">'
                f'</i></div>'
            )
        median = (
            _pct_html(band["median"])
            if band["median"] is not None else '<span class="dim">—</span>'
        )
        rows.append(
            f'<tr><td><span class="band-name">{_esc(band["label"])}</span></td>'
            f'<td class="num">{band["graded"]}</td>'
            f'<td><div class="band-rate"><span>{rate}</span>{meter}</div></td>'
            f'<td class="num">{median}</td></tr>'
        )
    edge = data["edge"]
    return (
        '<div class="scroll"><table class="table band-table" '
        'aria-label="Hit rate and median move by Scout score band">'
        '<thead><tr><th>Band</th><th class="num">Calls</th>'
        '<th>Hit rate</th><th class="num"><span class="hide-sm">Median </span>72h</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        f'<div class="record-edge"><strong>{_esc(edge["title"])}</strong>'
        f'<span>{_esc(edge["detail"])}</span></div>'
        '<p class="record-note">Bands use the best Scout score recorded at '
        'alert time. Small samples are shown as-is, not smoothed.</p>'
    )


def _record_calls_table(data: dict) -> str:
    if not data["calls"]:
        return _empty(
            "No calls to grade yet.",
            "The record fills when the scanner posts its first alert."
        )
    rows = []
    for call in data["calls"]:
        if call["delta"] is not None:
            move = _pct_html(call["delta"])
        elif call["status"] == "open" and call["so_far"] is not None:
            move = (
                f'{_pct_html(call["so_far"])}'
                '<span class="record-so-far">so far</span>'
            )
        else:
            move = '<span class="dim">—</span>'
        outcome = (
            _fmt_int(call["outcome"])
            if call["outcome"] is not None else "—"
        )
        latest = (
            f'{_fmt_int(call["latest"])}'
            f'<span class="record-time">{_time_ago(call["latest_dt"])}</span>'
            if call["latest"] is not None
            else '<span class="dim">no read</span>'
        )
        rows.append(
            f'<tr data-call data-outcome="{call["status"]}">'
            f'<td><a class="game-link" href="/game/{call["game_id"]}">'
            f'<span class="game-name">{_esc(call["name"])}</span>'
            f'<span class="game-id">#{call["game_id"]}</span></a></td>'
            f'<td class="num dim hide-sm">{_esc(call["alerted_day"])}</td>'
            f'<td>{_score(call["score"])}</td>'
            f'<td class="num hide-sm">{_fmt_int(call["baseline"])}</td>'
            f'<td class="num hide-sm">{outcome}</td>'
            f'<td class="num">{move}</td>'
            f'<td>{_record_status_badge(call["status"])}</td>'
            f'<td class="num hide-sm">{latest}</td></tr>'
        )
    return (
        '<div class="scroll"><table class="table record-table" '
        'id="record-table"><thead><tr><th>Game</th>'
        '<th class="num hide-sm">Alerted</th><th>Score</th>'
        '<th class="num hide-sm">Baseline</th>'
        '<th class="num hide-sm">72h</th><th class="num">Move</th>'
        '<th>Call</th><th class="num hide-sm">Latest</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _record_calls_foot(data: dict) -> str:
    foot = (
        f'of {_fmt_int(data["total"])} alerts · '
        f'{_fmt_int(data["open"])} open'
    )
    if data["unrateable"]:
        foot += f' · {_fmt_int(data["unrateable"])} without data'
    return foot


def render_scorecard() -> str:
    data = _track_record_data()
    strongest = data["strongest"]
    hit_rate = data["hit_rate"]
    wins_word = "win" if data["wins"] == 1 else "wins"
    misses_word = "miss" if data["misses"] == 1 else "misses"
    stats = "".join(
        (
            _stat(
                "Hit rate",
                f"{hit_rate:.0f}%" if hit_rate is not None else "—",
                f'{data["wins"]} {wins_word} · '
                f'{data["quiet"]} quiet · '
                f'{data["misses"]} {misses_word}',
            ),
            _stat(
                "Median 72h move",
                _pct_html(data["median_move"]),
                "baseline to 72h, graded calls only",
            ),
            _stat(
                "Calls graded",
                _fmt_int(len(data["graded"])),
                _record_calls_foot(data),
            ),
            _stat(
                "Best call",
                f'{strongest["delta"]:+.1f}%' if strongest else "—",
                _esc(strongest["name"]) if strongest else "no graded calls yet",
            ),
        )
    )
    bands = _record_bands(data)
    body = f"""
    <div class="shell">
      <header class="page-head">
        <p class="eyebrow">Accountability</p>
        <h1>Do the calls hold up?</h1>
        <p class="lede">Every alert from the last 30 days, graded 72 hours
          later against the bot's own snapshots. A +20% CCU move is a hit,
          a −20% move is a miss,
          and everything between is quiet. Open calls stay out of the
          hit-rate denominator until their window closes.</p>
      </header>
      <section class="stats" aria-label="Track record numbers">{stats}</section>
      <section class="panel">
        <div class="panel-head">
          <div><h2 class="panel-title">How the calls landed</h2>
            <p class="panel-sub">Fixed 72-hour outcome across every graded alert</p></div>
          {_badge(f'{len(data["graded"])} graded', 'blue')}
        </div>
        <div class="panel-body">{_record_distribution(data)}</div>
      </section>
      <div class="layout-2" style="margin-top:16px">
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Calibration plot</h2>
              <p class="panel-sub">Does a higher Scout score mean a better
                72-hour move? Every dot is one graded call.</p></div>
          </div>
          <div class="panel-body">
            {_record_scatter(data)}
            <div class="record-legend">
              <span class="record-key"><i class="record-swatch win"></i>Win</span>
              <span class="record-key"><i class="record-swatch quiet"></i>Quiet</span>
              <span class="record-key"><i class="record-swatch miss"></i>Miss</span>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <div><h2 class="panel-title">Score bands</h2>
              <p class="panel-sub">The same calls, grouped by alert score</p></div>
          </div>
          <div class="panel-body">{bands}</div>
        </section>
      </div>
      <section class="panel" style="margin-top:16px">
        <div class="panel-head">
          <div><h2 class="panel-title">Every call</h2>
            <p class="panel-sub">Newest first · open calls have not reached
              their 72-hour mark</p></div>
          <a class="panel-link" href="/games">Browse all games →</a>
        </div>
        <div class="panel-body">{_record_calls_table(data)}</div>
      </section>
      <p class="footnote">Baseline is the last snapshot at or before the
        alert; the 72h read is the first snapshot between 72 and 96 hours
        after it. Grading runs on the bot's own store, never a later
        re-fetch. All times UTC ·
        <a href="/watchlist">Tracking desk</a> ·
        <a href="/compare">Compare games</a></p>
    </div>"""
    return page("Scout — track record", body, active="record")


# ---------------------------------------------------------------------------
# Game detail
# ---------------------------------------------------------------------------

def _grade_badge(grade) -> str:
    if not grade:
        return '<span class="muted">Not gradeable yet</span>'
    outcome = grade.get("outcome")
    delta = grade.get("delta_pct")
    if outcome == "win":
        return f'{_badge("Call hit", "mint")} <span class="up">+{delta:.1f}%</span>'
    if outcome == "miss":
        return f'{_badge("Missed", "rose")} <span class="down">{delta:.1f}%</span>'
    if outcome == "quiet":
        return f'{_badge("Holding", "dim")} <span class="muted">{delta:.1f}%</span>'
    return '<span class="muted">Not gradeable yet</span>'


def _ai_card(ai) -> str:
    if not ai:
        return (
            '<div class="panel"><div class="panel-head"><div>'
            '<h2 class="panel-title">AI verdict</h2>'
            '<p class="panel-sub">Acquisition read</p></div></div>'
            '<div class="panel-body">'
            + _empty("No cached verdict.",
                     "The analyst runs when the bot evaluates an alert.")
            + "</div></div>"
        )
    verdict = ai.get("verdict") or "Data only"
    verdict_class = "mint"
    if "Avoid" in verdict:
        verdict_class = "rose"
    elif "Risky" in verdict:
        verdict_class = "amber"
    elif "Wait" in verdict or "Data" in verdict:
        verdict_class = "dim"
    confidence = ai.get("confidence")
    try:
        confidence = max(0, min(100, int(confidence)))
    except (TypeError, ValueError):
        confidence = 0

    def _bullets(raw):
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            items = [line for line in str(raw or "").splitlines() if line.strip()]
        if not isinstance(items, list):
            return ""
        rendered = "".join(
            f"<li>{_esc(item)}</li>" for item in items[:4] if str(item).strip()
        )
        return f'<ul class="bullets">{rendered}</ul>' if rendered else ""

    strengths = _bullets(ai.get("strengths"))
    risks = _bullets(ai.get("risks"))
    recommendation = (ai.get("recommendation") or "").strip()
    return f"""
    <div class="panel">
      <div class="panel-head"><div>
        <h2 class="panel-title">AI verdict</h2>
        <p class="panel-sub">Cached {_esc(_time_ago(ai.get("last_checked")))} ·
          never re-runs from this page</p></div>
        <span class="badge {verdict_class}">{_esc(verdict)}</span></div>
      <div class="panel-body">
        <div class="verdict">
          <div class="verdict-top"><span class="verdict-name">Confidence</span>
            <span class="muted">{confidence}%</span></div>
          <div class="confidence"><i style="width:{confidence}%"></i></div>
        </div>
        {f'<div class="mini-label">Strengths</div>{strengths}' if strengths else ''}
        {f'<div class="mini-label" style="margin-top:.7rem">Risks</div>{risks}' if risks else ''}
        {f'<p class="recommendation">{_esc(recommendation)}</p>' if recommendation else ''}
      </div>
    </div>"""


def _chart_panel(detail, chart_width=920, chart_height=300) -> str:
    ranges = detail["ranges"]
    default_key = next(
        (key for key in ("7d", "30d", "24h") if ranges.get(key)),
        _DEFAULT_RANGE,
    )
    if not any(ranges.values()):
        return f"""
        <section class="panel">
          <div class="chart-head"><div>
            <h2 class="panel-title">CCU history</h2>
            <p class="panel-sub">No snapshot history in the last 30 days yet.
              Watch the game to start recording it.</p></div></div>
          <div class="chart-empty">{_empty(
              "No snapshot history.",
              "The scanner writes a reading every time it sees this game."
          )}</div>
        </section>"""

    hours_by_key = dict(_RANGE_HOURS)
    raw_bench = detail.get("benchmark_raw") or {}
    benchmarks = {}
    tabs, panes = [], []
    for key, label in (("24h", "24 hours"), ("7d", "7 days"), ("30d", "30 days")):
        selected = "true" if key == default_key else "false"
        tabs.append(
            f'<button class="tab" type="button" role="tab" '
            f'id="tab-{detail["game_id"]}-{key}" '
            f'aria-controls="pane-{detail["game_id"]}-{key}" '
            f'data-range="{key}" aria-selected="{selected}">{label}</button>'
        )
        window = ranges.get(key, [])
        base_value = window[0][1] if window else None
        bench = _benchmark_range(raw_bench, hours_by_key.get(key, 24), base_value)
        benchmarks[key] = bench
        bench_points = bench["points"] if len(window) >= 2 else []
        wide = chart_svg(
            window,
            ident=f"{detail['game_id']}-{key}-wide",
            width=chart_width,
            height=chart_height,
            benchmark=bench_points,
        )
        narrow = chart_svg(
            window,
            ident=f"{detail['game_id']}-{key}-narrow",
            width=360,
            height=240,
            compact=True,
            benchmark=bench_points,
        )
        content = (
            f'<div class="chart-wrap chart-wide">{wide}</div>'
            f'<div class="chart-wrap chart-narrow">{narrow}</div>'
            if wide and narrow
            else f'<div class="chart-empty">{_empty(
                f"No readings in this {label} window.",
                "Longer windows may still have history."
            )}</div>'
        )
        panes.append(
            f'<div data-pane="{key}" role="tabpanel" '
            f'id="pane-{detail["game_id"]}-{key}" '
            f'aria-labelledby="tab-{detail["game_id"]}-{key}"'
            f'{" hidden" if key != default_key else ""}>{content}</div>'
        )

    latest = detail["latest_players"]
    readings = detail["history_count"]
    default_growth = detail["growth"].get(default_key)
    default_bench = benchmarks.get(default_key) or {}
    market_growth = default_bench.get("growth")
    vs_market = (
        round(default_growth - market_growth, 1)
        if isinstance(default_growth, (int, float))
        and isinstance(market_growth, (int, float))
        else None
    )
    growth_label = {"24h": "24h", "7d": "7d", "30d": "30d"}.get(default_key, "7d")
    market_note = (
        f" · vs Scout market {_spread_html(vs_market)}"
        if vs_market is not None else ""
    )
    legend = ""
    if default_bench.get("points") and len(ranges.get(default_key) or []) >= 2:
        legend = (
            '<div class="chart-key">'
            '<span><i class="key-solid"></i>This game</span>'
            '<span><i class="key-dash"></i>Scout market median</span>'
            f'<span>{_fmt_int(default_bench.get("games"))} games</span>'
            '</div>'
            '<p class="chart-basis">Median CCU path of the games Scout has '
            'identified, each indexed to its own first reading and scaled '
            'to this game\'s start.</p>'
        )
    return f"""
    <section class="panel" data-chart>
      <div class="chart-head">
        <div><h2 class="panel-title">CCU history</h2>
          <p class="panel-sub"><b>{_fmt_int(latest)} CCU latest</b> ·
            {_fmt_int(readings)} readings over 30 days ·
            {growth_label} {_pct_html(default_growth)}{market_note}</p></div>
        <div class="tabs" role="tablist" aria-label="Chart range">{''.join(tabs)}</div>
      </div>
      {legend}
      {''.join(panes)}
    </section>"""


_GAME_STALE_HOURS = 6


def _signed(value, digits=1) -> str:
    return f"{value:+.{digits}f}%"


def _game_read(detail: dict) -> dict:
    """A deterministic, local-only read of one game's dossier.

    No LLM, no network, no invented facts: every clause is a measurement
    already in `detail`. The badge is the strongest honest label — a stale
    reading outranks a momentum label, because a 10-hour-old spike is not
    a live signal.
    """
    series = [
        p for p in (detail.get("points") or [])
        if p.get("players") is not None and p.get("dt") is not None
    ]
    if not series:
        return {
            "label": "New",
            "tone": "dim",
            "headline": "No read yet — Scout has no snapshot history for this game.",
            "paragraph": (
                "There is no snapshot history in the last 30 days. Watch "
                "the game to start recording CCU, or let the next scan "
                "pick it up."
            ),
        }

    latest = series[-1]
    latest_dt = latest["dt"]
    growth = detail.get("growth") or {}
    g7 = growth.get("7d")
    g24 = growth.get("24h")
    latest_players = detail.get("latest_players")
    latest_visits = detail.get("latest_visits")
    peak = detail.get("peak_30d")

    off_peak = None
    if (
        isinstance(peak, (int, float)) and peak > 0
        and isinstance(latest_players, (int, float))
    ):
        off_peak = (float(latest_players) - float(peak)) / float(peak) * 100.0

    age_hours = (_now() - latest_dt).total_seconds() / 3600.0
    stale = age_hours > _GAME_STALE_HOURS

    if g7 is None:
        direction = None
    elif abs(g7) < 0.05:
        direction = "Flat over 7 days"
    elif g7 > 0:
        direction = f"Up {g7:.1f}% in 7 days"
    else:
        direction = f"Down {abs(g7):.1f}% in 7 days"

    if off_peak is None:
        position = None
    elif off_peak >= -1.0:
        position = "at its 30-day high"
    else:
        position = f"{abs(off_peak):.0f}% below its 30-day peak"

    if direction and position:
        headline = f"{direction}, {position}."
    elif direction:
        headline = f"{direction}."
    elif position:
        headline = f"{position[:1].upper()}{position[1:]}."
    else:
        headline = "Not enough history for a 7-day read yet."

    if stale:
        label, tone = "Stale data", "amber"
    elif g7 is None:
        label, tone = "Early read", "blue"
    elif g7 >= 10:
        label, tone = "Rising", "mint"
    elif g7 <= -10:
        label, tone = "Cooling", "rose"
    else:
        label, tone = "Steady", "blue"

    sentences = []
    if stale:
        sentences.append(
            f"The newest snapshot arrived {_time_ago(latest['timestamp'])}; "
            "treat this read as historical."
        )

    trend = []
    if g7 is not None:
        trend.append(f"{_signed(g7)} over 7 days")
    if g24 is not None:
        trend.append(f"{_signed(g24)} over 24 hours")
    if trend:
        sentences.append("CCU is " + " and ".join(trend) + ".")

    if latest_players is not None:
        reading = f"{_fmt_int(latest_players)} CCU"
        if isinstance(latest_visits, (int, float)):
            reading += f" and {_fmt_compact(latest_visits)} visits"
        sentences.append(reading + " at the latest reading.")

    alert = detail.get("alert") or {}
    if alert:
        score = alert.get("best_scout_score")
        when = _time_ago(alert.get("first_alerted"))
        if isinstance(score, (int, float)):
            sentences.append(f"Scout alerted it at {int(score)} ({when}).")
        else:
            sentences.append(f"Scout alerted it {when}.")

    charts = detail.get("charts") or []
    if detail.get("best_chart") is not None:
        best_row = min(
            charts, key=lambda r: int(r.get("position") or 10_000),
            default={},
        )
        chart_label = (best_row.get("sort_label") or "a Roblox chart").strip()
        sentences.append(
            f"Best chart position: #{int(detail['best_chart'])} on {chart_label}."
        )

    radar = detail.get("radar") or {}
    if radar:
        caught = _time_ago(radar.get("first_seen_ts"))
        sentences.append(
            f"The ascent radar first caught it {caught}."
        )

    watch = detail.get("watch") or {}
    if watch:
        count = int(watch.get("watch_count") or 0)
        sentences.append(
            f"{count} watch{'es' if count != 1 else ''} on record."
        )

    if not alert and not charts and not radar and not watch:
        sentences.append(
            "No alert, chart position or watch is on record yet — this is "
            "pure snapshot data."
        )

    if off_peak is not None and g7 is not None:
        if g7 > 0 and off_peak >= -1.0:
            sentences.append(
                "Trend and level agree: momentum is confirming at a high."
            )
        elif g7 > 0 and off_peak <= -15.0:
            sentences.append(
                "The move is real, but the game is still well below its peak."
            )
        elif g7 < 0 and off_peak <= -15.0:
            sentences.append("Both the trend and the level point down.")
        elif abs(g7) < 5.0:
            sentences.append("No decisive edge in either direction yet.")

    return {
        "label": label,
        "tone": tone,
        "headline": headline,
        "paragraph": " ".join(sentences),
    }


def _game_timeline(detail: dict) -> list[dict]:
    """Every dated event the bot holds for one game, newest first."""
    events = []

    def add(ts, title, note, tone):
        dt = _parse_ts(ts)
        if dt is None:
            return
        events.append({
            "dt": dt,
            "ts": ts,
            "title": title,
            "detail": note,
            "tone": tone,
        })

    alert = detail.get("alert") or {}
    if alert:
        score = alert.get("best_scout_score")
        note = f"Best score {int(score)}" if isinstance(score, (int, float)) else "Alert recorded"
        count = int(alert.get("alert_count") or 0)
        if count:
            note += f" · {count} alert{'s' if count != 1 else ''}"
        add(alert.get("first_alerted"), "Alert fired", note, "mint")

    watch = detail.get("watch") or {}
    if watch:
        count = int(watch.get("watch_count") or 0)
        auto = int(watch.get("auto_count") or 0)
        note = f"{count} watch{'es' if count != 1 else ''}"
        if auto:
            note += f" · {auto} auto"
        add(watch.get("first_watched"), "Tracking started", note, "blue")

    radar = detail.get("radar") or {}
    if radar:
        add(radar.get("first_seen_ts"), "Radar first catch",
            "Early breakout sighting", "rose")
        if str(radar.get("last_alert_ts") or "") != str(radar.get("first_seen_ts") or ""):
            add(
                radar.get("last_alert_ts"), "Radar escalation",
                f"{_fmt_int(radar.get('last_ccu'))} CCU", "rose",
            )

    project = detail.get("project") or {}
    if project:
        add(
            project.get("created_at"), "Portfolio project added",
            (project.get("role") or "Public project").strip(), "violet",
        )

    ai = detail.get("ai") or {}
    if ai:
        verdict = (ai.get("verdict") or "").strip() or "Verdict stored"
        confidence = ai.get("confidence")
        note = verdict
        if isinstance(confidence, (int, float)):
            note += f" · {int(confidence)}% confidence"
        add(ai.get("last_checked"), "AI verdict stored", note, "violet")

    for row in detail.get("charts") or []:
        position = row.get("position")
        if isinstance(position, (int, float)):
            add(
                row.get("recorded_at"), "Chart position",
                f"{row.get('sort_label') or 'Roblox chart'} · #{int(position)}",
                "amber",
            )

    series = [
        p for p in (detail.get("points") or [])
        if p.get("players") is not None and p.get("dt") is not None
    ]
    if series:
        latest = series[-1]
        if len(series) > 1:
            first = series[0]
            add(first.get("timestamp"), "First snapshot",
                f"{_fmt_int(first['players'])} CCU", "dim")

        peak_index = max(
            range(len(series)), key=lambda i: float(series[i]["players"])
        )
        if peak_index != len(series) - 1:
            peak = series[peak_index]
            add(peak.get("timestamp"), "30-day peak",
                f"{_fmt_int(peak['players'])} CCU", "mint")

        latest_note = f"{_fmt_int(latest['players'])} CCU"
        if latest.get("visits") is not None:
            latest_note += f" · {_fmt_compact(latest['visits'])} visits"
        add(latest.get("timestamp"), "Latest reading", latest_note, "dim")

    events.sort(key=lambda e: e["dt"], reverse=True)

    unique = []
    seen = set()
    for event in events:
        key = (event["title"], event["dt"], event["detail"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique[:12]


def _scout_read_html(read: dict) -> str:
    return (
        '<section class="read" data-scout-read>'
        '<div class="read-top"><span class="read-label">Scout read</span>'
        f'{_badge(read["label"], read["tone"])}</div>'
        f'<h2>{_esc(read["headline"])}</h2>'
        f'<p>{_esc(read["paragraph"])}</p></section>'
    )


def _timeline_html(events: list[dict]) -> str:
    if not events:
        return (
            '<p class="market-empty">No timeline events yet. The first '
            'scan, alert or watch will start the record.</p>'
        )
    items = []
    for event in events:
        items.append(
            f'<li class="tl {event["tone"]}"><span class="tl-dot" '
            f'aria-hidden="true"></span><div class="tl-body">'
            f'<div class="tl-head"><span class="tl-title">{_esc(event["title"])}</span>'
            f'<time>{_esc(_fmt_ts(event["ts"]))}</time></div>'
            f'<div class="tl-detail">{_esc(event["detail"])}</div></div></li>'
        )
    return f'<ol class="timeline">{"".join(items)}</ol>'


def render_game(game_id: int) -> str:
    detail = _game_detail(game_id)
    gid = detail["game_id"]
    name = detail["name"]
    read = _game_read(detail)
    timeline = _game_timeline(detail)
    if detail["points"]:
        detail["benchmark_raw"] = _benchmark_raw()
    alert = detail["alert"] or {}
    watch = detail["watch"] or {}
    project = detail["project"] or {}
    radar = detail["radar"] or {}
    ai = detail["ai"] or {}
    chart_rows = detail["charts"]
    initials = "".join(word[0] for word in str(name).split()[:2]).upper() or str(gid)[:2]

    tags = []
    if alert:
        tags.append(_badge("Alerted", "mint"))
    if detail["best_chart"] is not None:
        tags.append(_badge(f"Chart #{detail['best_chart']}", "amber"))
    if watch:
        tags.append(_badge("Watched", "blue"))
    if project:
        tags.append(_badge("Portfolio", "violet"))
    if radar:
        tags.append(_badge("Radar", "rose"))

    first_alerted = detail["first_alerted"]
    grade = detail["grade"]
    stats = "".join(
        (
            f'<div class="mini"><div class="mini-label">Latest CCU</div>'
            f'<div class="mini-value">{_fmt_int(detail["latest_players"])}'
            f" <small>CCU</small></div></div>",
            f'<div class="mini"><div class="mini-label">Visits</div>'
            f'<div class="mini-value">{_fmt_compact(detail["latest_visits"])}'
            f"</div></div>",
            f'<div class="mini"><div class="mini-label">7d growth</div>'
            f'<div class="mini-value">{_pct_html(detail["growth"].get("7d"))}'
            f"</div></div>",
            f'<div class="mini"><div class="mini-label">30d peak</div>'
            f'<div class="mini-value">{_fmt_compact(detail["peak_30d"])}'
            f"</div></div>",
        )
    )

    chart_rows_html = "".join(
        f'<div class="kv"><span class="kv-k">{_esc(row.get("sort_label") or "Chart")}'
        f'</span><span class="kv-v">#{int(row.get("position") or 0)}</span></div>'
        for row in chart_rows
    ) or '<p class="muted" style="font-size:.8rem;margin:.4rem 0">Not on a chart in the last 30 days.</p>'

    ai_verdict = (ai.get("verdict") or "").strip()
    watch_meta = "—"
    if watch:
        watch_meta = (
            f'{int(watch.get("watch_count") or 0)} watches'
            f' · {int(watch.get("auto_count") or 0)} auto'
        )

    body = f"""
    <div class="shell">
      <div class="crumbs"><a href='/games'>← all games</a>
        <span>/</span><span>#{gid}</span></div>
      <header class="game-head">
        <div class="game-mark" aria-hidden="true">{_esc(initials)}</div>
        <div class="game-head-main">
          <h1>{_esc(name)}</h1>
          <div class="game-meta">
            <span>game_id {gid}</span>
            <span>·</span>
            <a href="https://www.roblox.com/games/{gid}" rel="noopener">Open on
              Roblox ↗</a>
            {f'<span>·</span><a href="{_esc(project.get("project_url"))}" rel="noopener">Project ↗</a>' if project.get("project_url") else ''}
          </div>
          <div style="display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.65rem">
            {''.join(tags) or _badge('New to Scout', 'dim')}
          </div>
        </div>
        <div class="game-actions">
          <a class="btn btn-ghost" href="/api/v1/series?ids={gid}&hours=168">
            this series as JSON</a>
        </div>
      </header>
      <section class="mini-stats" aria-label="Game statistics">{stats}</section>
      {_scout_read_html(read)}
      <div class="detail">
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          {_chart_panel(detail)}
          <section class="panel">
            <div class="panel-head"><div>
              <h2 class="panel-title">Signals</h2>
              <p class="panel-sub">What the bot knows locally</p></div></div>
            <div class="panel-body">
              <div class="kv"><span class="kv-k">Call grade</span>
                <span class="kv-v">{_grade_badge(grade)}</span></div>
              <div class="kv"><span class="kv-k">Watch state</span>
                <span class="kv-v">{_esc(watch_meta)}</span></div>
              <div class="kv"><span class="kv-k">Last seen</span>
                <span class="kv-v">{_esc(_time_ago(detail["last_seen"]))}</span></div>
              <div class="kv"><span class="kv-k">Snapshots (30d)</span>
                <span class="kv-v">{_fmt_int(detail["history_count"])}</span></div>
              {chart_rows_html}
            </div>
          </section>
          {f'<section class="panel"><div class="panel-head"><div><h2 class="panel-title">About</h2><p class="panel-sub">{_esc(project.get("role") or "Portfolio project")}</p></div></div><div class="panel-body"><p style="margin:0;color:var(--muted);font-size:.85rem;line-height:1.6">{_esc(project.get("description"))}</p></div></section>' if project.get("description") else ''}
        </div>
        <aside class="rail">
          <section class="panel">
            <div class="panel-head"><div>
              <h2 class="panel-title">Scout record</h2>
              <p class="panel-sub">The call the bot made</p></div></div>
            <div class="panel-body">
              <div style="display:flex;align-items:baseline;justify-content:space-between;gap:1rem">
                <span style="font-size:2.1rem;font-weight:750;letter-spacing:-.04em">
                  {_fmt_int(alert.get("best_scout_score"))}</span>
                <span class="muted" style="font-size:.75rem">best Scout score</span>
              </div>
              <div class="kv"><span class="kv-k">Alerts</span>
                <span class="kv-v">{int(alert.get("alert_count") or 0)}</span></div>
              <div class="kv"><span class="kv-k">First alerted</span>
                <span class="kv-v">{_esc(_time_ago(first_alerted))}</span></div>
              <div class="kv"><span class="kv-k">Call grade</span>
                <span class="kv-v">{_grade_badge(grade)}</span></div>
              <div class="kv"><span class="kv-k">AI verdict</span>
                <span class="kv-v">{_esc(ai_verdict) if ai_verdict else "—"}</span></div>
            </div>
          </section>
          <section class="panel">
            <div class="panel-head"><div>
              <h2 class="panel-title">Radar</h2>
              <p class="panel-sub">Early breakout state</p></div></div>
            <div class="panel-body">
              {f'<div class="kv"><span class="kv-k">Last catch</span><span class="kv-v">{_esc(_time_ago(radar.get("last_alert_ts")))}</span></div><div class="kv"><span class="kv-k">CCU at catch</span><span class="kv-v">{_fmt_int(radar.get("last_ccu"))}</span></div><div class="kv"><span class="kv-k">First seen</span><span class="kv-v">{_esc(_time_ago(radar.get("first_seen_ts")))}</span></div>' if radar else '<p class="muted" style="font-size:.8rem;margin:.35rem 0">No radar catch yet. The radar scans every 5 minutes.</p>'}
            </div>
          </section>
          {_ai_card(ai)}
        </aside>
      </div>
      <section class="panel timeline-panel" data-timeline>
        <div class="panel-head">
          <div><h2 class="panel-title">Signal timeline</h2>
            <p class="panel-sub">The dated events Scout holds for this
              game, newest first</p></div>
        </div>
        <div class="panel-body">{_timeline_html(timeline)}</div>
      </section>
    </div>"""
    return page(f"Scout — {name}", body, active="games")


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def _verdict_badge(ai) -> str:
    verdict = (ai or {}).get("verdict")
    if not verdict:
        return '<span class="dim">—</span>'
    tone = "mint"
    if "Avoid" in verdict:
        tone = "rose"
    elif "Risky" in verdict:
        tone = "amber"
    elif "Wait" in verdict or "Data" in verdict:
        tone = "dim"
    return _badge(verdict, tone)


def _compare_table(left, right) -> str:
    """At-a-glance metrics; the stronger value gets the mint treatment."""
    la = left["alert"] or {}
    lb = right["alert"] or {}
    entries = []

    def add_numeric(label, av, bv, formatter, higher_is_better=True):
        a_num = float(av) if isinstance(av, (int, float)) else None
        b_num = float(bv) if isinstance(bv, (int, float)) else None
        a_best = b_best = False
        if a_num is not None and b_num is not None and a_num != b_num:
            if higher_is_better:
                a_best, b_best = a_num > b_num, b_num > a_num
            else:
                a_best, b_best = a_num < b_num, b_num < a_num
        entries.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td class='{'best' if a_best else ''}'>{formatter(av)}</td>"
            f"<td class='{'best' if b_best else ''}'>{formatter(bv)}</td></tr>"
        )

    def add_text(label, a_value, b_value):
        entries.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td>{a_value}</td><td>{b_value}</td></tr>"
        )

    add_numeric("Latest CCU", left["latest_players"], right["latest_players"], _fmt_int)
    add_numeric("7d growth", left["growth"].get("7d"),
                right["growth"].get("7d"), _pct_html)
    add_numeric("30d peak", left["peak_30d"], right["peak_30d"], _fmt_compact)
    add_numeric("Visits", left["latest_visits"], right["latest_visits"], _fmt_compact)
    add_numeric("Best Scout score", la.get("best_scout_score"),
                lb.get("best_scout_score"), _fmt_int)
    add_numeric("Alerts", la.get("alert_count"),
                lb.get("alert_count"), _fmt_int)
    add_numeric(
        "Best chart", left["best_chart"], right["best_chart"],
        lambda value: f"#{int(value)}" if isinstance(value, (int, float)) else "—",
        higher_is_better=False,
    )

    def watch_text(detail):
        watch = detail["watch"] or {}
        if not watch:
            return '<span class="dim">—</span>'
        return _esc(f'{int(watch.get("watch_count") or 0)} watches')

    add_text("Watch state", watch_text(left), watch_text(right))
    add_text("Call grade", _grade_badge(left["grade"]), _grade_badge(right["grade"]))
    add_text("AI verdict", _verdict_badge(left["ai"]), _verdict_badge(right["ai"]))
    add_text(
        "Radar catch",
        _esc(_time_ago((left["radar"] or {}).get("last_alert_ts")))
        if left["radar"] else '<span class="dim">—</span>',
        _esc(_time_ago((right["radar"] or {}).get("last_alert_ts")))
        if right["radar"] else '<span class="dim">—</span>',
    )

    return f"""
    <div class="scroll"><table class="table compare-table">
      <thead><tr><th>Metric</th><th>{_esc(left["name"])}</th>
        <th>{_esc(right["name"])}</th></tr></thead>
      <tbody>{''.join(entries)}</tbody>
    </table></div>"""


def _compare_col(detail) -> str:
    gid = detail["game_id"]
    name = detail["name"]
    initials = "".join(word[0] for word in str(name).split()[:2]).upper() or str(gid)[:2]
    tags = []
    if detail["alert"]:
        tags.append(_badge("Alerted", "mint"))
    if detail["best_chart"] is not None:
        tags.append(_badge(f"Chart #{detail['best_chart']}", "amber"))
    if detail["watch"]:
        tags.append(_badge("Watched", "blue"))
    if detail["project"]:
        tags.append(_badge("Portfolio", "violet"))
    return f"""
    <div class="compare-col">
      <header class="compare-head">
        <div class="game-mark sm" aria-hidden="true">{_esc(initials)}</div>
        <div style="min-width:0">
          <h2><a href="/game/{gid}">{_esc(name)}</a></h2>
          <div class="game-meta"><span>#{gid}</span>
            <span style="display:flex;gap:.25rem;flex-wrap:wrap">
              {''.join(tags) or _badge('New to Scout', 'dim')}</span></div>
        </div>
      </header>
      {_chart_panel(detail, chart_width=560, chart_height=240)}
      <a class="btn btn-ghost compare-link" href="/game/{gid}">
        Full dossier →</a>
    </div>"""


def render_compare(a=None, b=None) -> str:
    index = _game_index()
    rows = index["rows"]
    header = """
    <header class="page-head">
      <p class="eyebrow">Head to head</p>
      <h1>Two games. One decision.</h1>
      <p class="lede">Line up any two scouted games and compare the numbers
        that matter — current CCU, trajectory, chart position, the bot's
        alert record and its AI read. Every number is local.</p>
    </header>"""

    if not rows:
        body = f"""<div class="shell">{header}
        <section class="panel"><div class="panel-body">{_empty(
            "Nothing to compare yet.",
            "The console fills after the first scan or portfolio import."
        )}</div></section></div>"""
        return page("Scout — compare", body, active="compare")

    by_id = {row["game_id"]: row for row in rows}
    requested_a = _as_int(a)
    requested_b = _as_int(b)
    selected_a = by_id.get(requested_a) if requested_a is not None else None
    if selected_a is None:
        selected_a = rows[0]

    selected_b = None
    if requested_b is not None:
        candidate = by_id.get(requested_b)
        if candidate is not None and candidate["game_id"] != selected_a["game_id"]:
            selected_b = candidate
    if selected_b is None:
        selected_b = next(
            (row for row in rows if row["game_id"] != selected_a["game_id"]),
            None,
        )

    unknown = []
    for raw in (a, b):
        value = _as_int(raw)
        if raw is not None and (value is None or value not in by_id):
            unknown.append(str(raw))
    notice = (
        f'<p class="compare-note">Game id {_esc(", ".join(unknown))} was not '
        "found — showing the closest local match.</p>"
        if unknown else ""
    )

    option_rows = sorted(rows, key=lambda row: row["name"].casefold())

    def options(selected_id):
        rendered = []
        for row in option_rows:
            score = row["score"]
            suffix = f" · {int(score)}" if isinstance(score, (int, float)) else ""
            chosen = " selected" if row["game_id"] == selected_id else ""
            rendered.append(
                f'<option value="{row["game_id"]}"{chosen}>'
                f'{_esc(row["name"])}{_esc(suffix)}</option>'
            )
        return "".join(rendered)

    selected_b_id = selected_b["game_id"] if selected_b else None
    swap = (
        f'<a class="btn btn-ghost" href="/compare?a={selected_b_id}'
        f'&b={selected_a["game_id"]}">⇄ Swap</a>'
        if selected_b else ""
    )
    if a is None and b is None:
        default_note = (
            '<p class="compare-note">Showing the two highest-scoring games — '
            "pick any pair above.</p>"
            if len(rows) >= 2 else
            '<p class="compare-note">Only one game is on the board — the '
            "second side unlocks once another game is identified.</p>"
        )
    else:
        default_note = notice

    picker = f"""
    <form class="panel compare-form" data-compare-form method="get"
      action="/compare">
      <label class="compare-field"><span>Game A</span>
        <select name="a" aria-label="First game">{options(selected_a["game_id"])}</select>
      </label>
      <span class="compare-vs">vs</span>
      <label class="compare-field"><span>Game B</span>
        <select name="b" aria-label="Second game"
          {'disabled' if not selected_b else ''}>
          {options(selected_b_id)}
        </select>
      </label>
      <button class="btn btn-primary" type="submit">Compare</button>
      {swap}
    </form>
    {default_note}"""

    bench_raw = _benchmark_raw()

    if selected_b is None:
        single = _game_detail(selected_a["game_id"])
        single["benchmark_raw"] = bench_raw
        content = f"""
        <div class="compare-grid single">{_compare_col(single)}</div>
        <section class="panel" style="margin-top:16px"><div class="panel-body">
          {_empty("Pick a second game.", "At least two identified games are needed for a comparison.")}
        </div></section>"""
    else:
        left = _game_detail(selected_a["game_id"])
        right = _game_detail(selected_b["game_id"])
        left["benchmark_raw"] = bench_raw
        right["benchmark_raw"] = bench_raw
        content = f"""
        <section class="panel" style="margin-top:16px">
          <div class="panel-head"><div>
            <h2 class="panel-title">At a glance</h2>
            <p class="panel-sub">The stronger number in each row is mint</p>
          </div><a class="panel-link" href="/games">Browse all →</a></div>
          <div class="panel-body">{_compare_table(left, right)}</div>
        </section>
        <div class="compare-grid">{_compare_col(left)}{_compare_col(right)}</div>"""

    body = f'<div class="shell">{header}{picker}{content}</div>'
    return page("Scout — compare games", body, active="compare")
