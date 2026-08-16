"""Public portfolio API.

Serves the live data behind your Framer portfolio cards:

    GET /api/v1/portfolio                  all public projects + latest stats
    GET /api/v1/portfolio/{game_id}        one project
    GET /api/v1/portfolio/{game_id}/stats  current stats + growth
    GET /api/v1/portfolio/{game_id}/history  time series for charts
    GET /api/v1/health                     uptime / staleness probe

Responses are served from a short-lived in-memory cache so a traffic burst
never translates into a burst of Roblox requests.

Run:  uvicorn portfolio_api:app --host 0.0.0.0 --port 8000
"""

import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import (
    PORTFOLIO_API_HOST,
    PORTFOLIO_API_PORT,
    PORTFOLIO_CACHE_SECONDS,
    PORTFOLIO_CORS_ORIGINS,
)
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
    return None


# ---- payload builders ----

def _game_stats(game_id):
    latest = get_latest_snapshot(game_id)
    if latest is None:
        return None
    return {
        "players": latest["players"],
        "visits": latest["visits"],
        "favorites": latest["favorites"],
        "likes": latest["likes"],
        "dislikes": latest["dislikes"],
        "like_ratio": latest["like_ratio"],
        "peak_players_7d": get_peak_players(game_id, hours=24 * 7),
        "growth": {
            "players_1h": compute_growth(game_id, 1)["players"],
            "players_6h": compute_growth(game_id, 6)["players"],
            "players_24h": compute_growth(game_id, 24)["players"],
            "players_7d": compute_growth(game_id, 24 * 7)["players"],
        },
        "updated_at": latest["timestamp"],
    }


def _portfolio_payload():
    projects = []
    latest_overall = None
    for game in list_portfolio_games(visible_only=True):
        stats = _game_stats(game["game_id"])
        discord = get_latest_discord_snapshot(game["game_id"])
        if stats and stats["updated_at"]:
            ts = stats["updated_at"]
            if latest_overall is None or ts > latest_overall:
                latest_overall = ts
        projects.append({
            "game_id": game["game_id"],
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
    projects = list_portfolio_games(visible_only=True)
    fresh = 0
    for game in projects:
        latest = get_latest_snapshot(game["game_id"])
        if latest:
            fresh += 1
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
    payload, _ = _cached(("stats", game_id), PORTFOLIO_CACHE_SECONDS)
    if payload is None:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    return JSONResponse(content=payload)


@app.get("/api/v1/portfolio/{game_id}/history")
def get_project_history(game_id: int, period: str = "7d"):
    hours = {"1d": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}.get(period, 24 * 7)
    payload = _history_payload(game_id, hours)
    if not payload["points"] and get_portfolio_game(game_id) is None:
        return JSONResponse(content={"detail": "not found"}, status_code=404)
    return JSONResponse(content=payload)


def run():
    import uvicorn
    uvicorn.run(app, host=PORTFOLIO_API_HOST, port=PORTFOLIO_API_PORT)


if __name__ == "__main__":
    run()
