"""Public portfolio API.

Serves the live data behind your Framer portfolio cards:

    GET /api/v1/portfolio                  all public projects + latest stats
    GET /api/v1/portfolio/{game_id}        one project
    GET /api/v1/portfolio/{game_id}/stats  current stats + growth
    GET /api/v1/portfolio/{game_id}/history  time series for charts
    GET /api/v1/health                     uptime / staleness probe
    GET /api/v1/leaderboard                Scout leaderboard JSON (ETag/304)

    GET /dashboard                         live market board (HTML)
    GET /games                             searchable game index (HTML)
    GET /compare?a=…&b=…                   head-to-head comparison (HTML)
    GET /watchlist                         tracking desk (HTML)
    GET /scorecard                         72-hour track record (HTML)
    GET /game/{id}                         one game's local dossier (HTML)

Responses are served from a short-lived in-memory cache so a traffic burst
never translates into a burst of Roblox requests.

Run:  uvicorn portfolio_api:app --host 0.0.0.0 --port 8000
"""

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import (
    PORTFOLIO_API_HOST,
    PORTFOLIO_API_PORT,
    PORTFOLIO_CACHE_SECONDS,
    PORTFOLIO_CORS_ORIGINS,
)
import web_ui
from portfolio_db import (
    compute_growth,
    get_latest_discord_snapshot,
    get_latest_snapshot,
    get_peak_players,
    get_portfolio_game,
    get_snapshots,
    list_portfolio_games,
)

app = FastAPI(title="Game Scout Portfolio API", version="1.0.0")

if PORTFOLIO_CORS_ORIGINS.strip() == "*":
    allow_origins = ["*"]
else:
    allow_origins = [
        o.strip() for o in PORTFOLIO_CORS_ORIGINS.split(",") if o.strip()
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---- tiny TTL cache (thread-safe enough for read-mostly responses) ----

_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl):
    """Return (value, from_cache). Builds value via callback when stale."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry["at"] < ttl:
            return entry["value"], True

    value = _build(key)

    with _cache_lock:
        _cache[key] = {"at": now, "value": value}
        # Opportunistic sweep so the cache can't grow unbounded.
        for stale_key in [k for k, e in _cache.items() if now - e["at"] > ttl * 10]:
            _cache.pop(stale_key, None)
    return value, False


def _build(key):
    """Recompute a cached payload from the database."""
    kind, ident = key
    if kind == "portfolio":
        return _portfolio_payload()
    if kind == "stats":
        return _stats_payload(int(ident))
    if kind == "leaderboard":
        limit_s, days_s = str(ident).split(":", 1)
        return _build_leaderboard_payload(int(limit_s), int(days_s))
    if kind == "series":
        ids_s, hours_s = str(ident).split(":", 1)
        ids = [int(x) for x in ids_s.split(",") if x]
        return _build_series_payload(ids, int(hours_s))
    return None


# ---- payload builders ----

def _stats_from_rows(rows, now=None):
    """The project "stats" block, computed from one series read.

    The old builder made ~7 connections per project (latest snapshot,
    a 7d peak fetch, four growth windows each re-reading the table)
    plus one Discord lookup. Everything here derives from the same ASC
    row list, with the same branching: no rows → None; every growth
    window needs ≥2 points → None; 7d peak is the max players inside
    the window, None when the window is empty.
    """
    if not rows:
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    def _within(hours):
        """Rows inside the last `hours` (chronological)."""
        cutoff = (now - timedelta(hours=hours)).isoformat(sep=" ")
        return [r for r in rows if (r["timestamp"] or "") >= cutoff]

    def _pct(a, b):
        if a is None or b is None or a == 0:
            return None
        return round((b - a) / a * 100.0, 2)

    def _growth(hours):
        window = _within(hours)
        if len(window) < 2:
            return None
        return _pct(window[0]["players"], window[-1]["players"])

    week = _within(24 * 7)
    peaks = [r["players"] for r in week if r["players"] is not None]

    return {
        "players": rows[-1]["players"],
        "visits": rows[-1]["visits"],
        "favorites": rows[-1]["favorites"],
        "likes": rows[-1]["likes"],
        "dislikes": rows[-1]["dislikes"],
        "like_ratio": rows[-1]["like_ratio"],
        "peak_players_7d": max(peaks) if peaks else None,
        "growth": {
            "players_1h": _growth(1),
            "players_6h": _growth(6),
            "players_24h": _growth(24),
            "players_7d": _growth(24 * 7),
        },
        "updated_at": rows[-1]["timestamp"],
    }


def _game_stats(game_id):
    """Single-project stats: one series read serves every field."""
    from portfolio_db import get_snapshots_bulk

    rows = get_snapshots_bulk([game_id]).get(int(game_id), [])
    return _stats_from_rows(rows)


def _portfolio_payload():
    from portfolio_db import (
        get_latest_discord_snapshots_bulk,
        get_snapshots_bulk,
    )

    games = list_portfolio_games(visible_only=True)
    ids = [g["game_id"] for g in games]
    # Two queries total: all series, then the Discord ledger. Used to be
    # ~7 connections + one discord lookup PER project.
    series = get_snapshots_bulk(ids)
    discord_map = get_latest_discord_snapshots_bulk(ids)

    projects = []
    latest_overall = None
    for game in games:
        gid = game["game_id"]
        stats = _stats_from_rows(series.get(gid, []))
        discord = discord_map.get(gid) or get_latest_discord_snapshot(gid)
        if stats and stats["updated_at"]:
            ts = stats["updated_at"]
            if latest_overall is None or ts > latest_overall:
                latest_overall = ts
        projects.append({
            "game_id": gid,
            "name": game["display_name"],
            "role": game["role"],
            "description": game["description"],
            "project_url": game["project_url"],
            "roblox_url": game["roblox_url"],
            "discord_url": game["discord_url"],
            "thumbnail_url": game["thumbnail_url"],
            "stats": stats,
            "discord": discord,
        })
    return {
        "updated_at": latest_overall,
        "source": "Roblox Games API + Game Scout snapshots",
        "projects": projects,
    }


def _stats_payload(game_id):
    game = get_portfolio_game(game_id)
    if game is None:
        return None
    stats = _game_stats(game_id)
    return {
        "game_id": game["game_id"],
        "name": game["display_name"],
        "role": game["role"],
        "stats": stats,
        "discord": get_latest_discord_snapshot(game_id),
    }


def _build_series_payload(game_ids, hours):
    """Bulk time-series payload for the dashboard's multi-chart views."""
    from portfolio_db import get_snapshots_bulk

    groups = get_snapshots_bulk(game_ids, hours=hours)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_hours": hours,
        # Requested games stay in request order; a game with no data in
        # the window keeps its key with an empty points list, so charts
        # can render a gap instead of a missing series.
        "series": [
            {"game_id": gid, "points": groups.get(gid, [])} for gid in game_ids
        ],
    }


def _history_payload(game_id, hours):
    rows = get_snapshots(game_id, hours=hours)
    return {
        "game_id": game_id,
        "period_hours": hours,
        "points": [
            {
                "timestamp": r["timestamp"],
                "players": r["players"],
                "visits": r["visits"],
                "favorites": r["favorites"],
                "likes": r["likes"],
                "dislikes": r["dislikes"],
            }
            for r in rows
        ],
    }


# ---- endpoints ----

@app.get("/api/v1/health")
def health():
    from portfolio_db import get_latest_snapshots_bulk

    projects = list_portfolio_games(visible_only=True)
    # One bulk lookup instead of one connection per project.
    latest_map = get_latest_snapshots_bulk(
        [g["game_id"] for g in projects]
    )
    fresh = sum(1 for g in projects if latest_map.get(g["game_id"]))
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "projects": len(projects),
        "projects_with_data": fresh,
    }


@app.get("/api/v1/portfolio")
def get_portfolio():
    payload, _ = _cached(("portfolio", 0), PORTFOLIO_CACHE_SECONDS)
    return JSONResponse(content=payload)


@app.get("/api/v1/portfolio/{game_id}")
def get_project(game_id: int):
    game = get_portfolio_game(game_id)
    if game is None or not game["visible"]:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    stats, _ = _cached(("stats", game_id), PORTFOLIO_CACHE_SECONDS)
    return JSONResponse(content={
        "game_id": game["game_id"],
        "name": game["display_name"],
        "role": game["role"],
        "description": game["description"],
        "project_url": game["project_url"],
        "roblox_url": game["roblox_url"],
        "discord_url": game["discord_url"],
        "thumbnail_url": game["thumbnail_url"],
        "stats": stats["stats"] if stats else None,
        "discord": stats["discord"] if stats else None,
    })


@app.get("/api/v1/portfolio/{game_id}/stats")
def get_project_stats(game_id: int):
    game = get_portfolio_game(game_id)
    # Hidden games must not be reachable through the public API, same as
    # the portfolio list/detail endpoints.
    if game is None or not game["visible"]:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    payload, _ = _cached(("stats", game_id), PORTFOLIO_CACHE_SECONDS)
    if payload is None:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    return JSONResponse(content=payload)


@app.get("/api/v1/portfolio/{game_id}/history")
def get_project_history(game_id: int, period: str = "7d"):
    hours = {"1d": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}.get(period, 24 * 7)
    game = get_portfolio_game(game_id)
    # Hidden games must not be reachable through the public API.
    if game is None or not game["visible"]:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    payload = _history_payload(game_id, hours)
    if not payload["points"]:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    return JSONResponse(content=payload)


@app.get("/api/v1/series")
def get_series(request: Request, ids: str, hours: int = 168):
    """Bulk time series: `?ids=1,2,3&hours=168` — one query per request.

    Reads only the game_snapshots store (no Roblox requests) and is safe
    to expose publicly: raw player/visit counters the portfolio API
    already serves per-game. Args are clamped (≤20 ids, hours 24–2160 =
    90 days) and the result is served from the same short-TTL cache with
    an ETag like the leaderboard — a dashboard with several charts used
    to cost one DB round-trip per chart and now costs one cached,
    304-capable call for all of them.
    """
    try:
        parsed = [int(x) for x in str(ids).replace(" ", "").split(",") if x]
    except (TypeError, ValueError):
        return JSONResponse(content={"detail": "ids must be integers"}, status_code=400)
    parsed = list(dict.fromkeys(parsed))  # dedupe, keep client order
    parsed = parsed[:20]
    if not parsed:
        return JSONResponse(content={"detail": "ids required"}, status_code=400)
    hours = max(24, min(int(hours), 2160))
    key_ids = ",".join(str(g) for g in parsed)
    payload, _ = _cached(("series", f"{key_ids}:{hours}"), PORTFOLIO_CACHE_SECONDS)
    etag = _data_etag({
        "period_hours": payload.get("period_hours"),
        "series": payload.get("series"),
    })
    headers = {
        "ETag": etag,
        "Cache-Control": f"public, max-age={PORTFOLIO_CACHE_SECONDS}",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return JSONResponse(content=None, status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)
@app.get("/api/v1/leaderboard")
def get_scout_leaderboard(
    request: Request, limit: int = 10, days: int = 30
):
    """JSON feed of the bot's scout leaderboard, same data as /leaderboard.

    Reads the alerted_games store — pure local data, no Roblox requests —
    and is safe to expose publicly: it contains only game ids, names,
    scores and timestamps the bot already alerts into a public channel.
    Cached with the same short TTL as the portfolio payloads.

    ETag: the fingerprint covers only the leaderboard *data* (entries +
    window), never the generated_at stamp — so a rebuild that lands the
    same ranking keeps the same ETag and dashboards with If-None-Match
    get a real 304 with no body, instead of re-pulling the feed.
    """
    from database import top_alerted_by_scout_score

    limit = max(1, min(int(limit), 50))
    days = max(0, min(int(days), 365))
    payload, _ = _cached(("leaderboard", f"{limit}:{days}"), PORTFOLIO_CACHE_SECONDS)
    etag = _leaderboard_etag(payload)
    headers = {
        "ETag": etag,
        "Cache-Control": f"public, max-age={PORTFOLIO_CACHE_SECONDS}",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return JSONResponse(content=None, status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


def _data_etag(basis: dict) -> str:
    """Strong ETag over a payload's data fields only.

    generated_at is deliberately excluded at every call site: it changes
    on every cache rebuild even when the data is identical, which would
    turn the ETag into a stopwatch instead of a fingerprint.
    """
    digest = hashlib.sha1(
        json.dumps(basis, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f'"{digest}"'


def _leaderboard_etag(payload: dict) -> str:
    """Strong ETag over the leaderboard's data fields only."""
    return _data_etag({
        "window_days": payload.get("window_days"),
        "entries": payload.get("entries"),
    })


def _etag_matches(header_value, etag: str) -> bool:
    """If-None-Match comparison, tolerant of lists and the W/ weak prefix."""
    if not header_value:
        return False
    bare = etag.replace('W/', "")
    for candidate in header_value.split(","):
        if candidate.strip().replace('W/', "").strip() == bare:
            return True
    return False


def _build_leaderboard_payload(limit: int, days: int):
    from database import top_alerted_by_scout_score

    rows = top_alerted_by_scout_score(limit=limit, days=days)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "entries": [
            {
                "game_id": gid,
                "name": name,
                "best_scout_score": score,
                "alert_count": count,
                "first_alerted": first,
            }
            for (gid, name, score, count, first) in rows
        ],
    }


@app.get("/", response_class=HTMLResponse)
def root():
    """The board is the front door."""
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Live market board: the bot's own opportunity, momentum and radar data.

    The portfolio payload is served from the same short-TTL cache the JSON
    feeds use; every other section is a local read of the bot's SQLite
    stores (one bulk history query serves all the sparklines).
    """
    portfolio, _ = _cached(("portfolio", 0), PORTFOLIO_CACHE_SECONDS)
    return HTMLResponse(content=web_ui.render_dashboard(portfolio))


@app.get("/market", response_class=HTMLResponse)
def market_page():
    """Market weather: breadth, move distribution and drawdowns.

    Pure local aggregation over the bot's own snapshot store — the same
    30-day history every other console view reads, never Roblox.
    """
    return HTMLResponse(content=web_ui.render_market())


@app.get("/games", response_class=HTMLResponse)
def games_selector():
    """Searchable index of every game the bot has identified.

    Alerts, watches and portfolio projects are merged locally; the page is
    server-rendered and filtered/sorted in the browser with the data it
    already carries. No per-game queries, no network.
    """
    return HTMLResponse(content=web_ui.render_games())


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page():
    """The tracking desk: watched games, freshness and momentum."""
    return HTMLResponse(content=web_ui.render_watchlist())


@app.get("/scorecard", response_class=HTMLResponse)
def scorecard_page():
    """The bot's own report card: every alert graded at a fixed 72h horizon."""
    return HTMLResponse(content=web_ui.render_scorecard())


@app.get("/compare", response_class=HTMLResponse)
def compare_page(a: Optional[int] = None, b: Optional[int] = None):
    """Head-to-head view of any two games, all from local stores."""
    return HTMLResponse(content=web_ui.render_compare(a, b))


@app.get("/game/{game_id}", response_class=HTMLResponse)
def game_page(game_id: int):
    """One game's local dossier: history, alert record, charts, AI verdict."""
    return HTMLResponse(content=web_ui.render_game(game_id))


# ---- server-rendered visuals ----

def _sparkline_svg(players, width=240, height=56, color="#2f6fed"):
    """Compatibility wrapper: the sparkline lives in web_ui now."""
    return web_ui.sparkline_svg(players, width=width, height=height, color=color)


def run():
    """Start the API with uvicorn (python portfolio_api.py)."""
    import uvicorn

    uvicorn.run(app, host=PORTFOLIO_API_HOST, port=PORTFOLIO_API_PORT)


if __name__ == "__main__":
    run()
