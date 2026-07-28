"""
Roblox Search API client.

Provides keyword-based game discovery using multiple Roblox API endpoints
as fallbacks. The primary search endpoint (/v1/games/list) is known to return
404 on some routes, so we try several strategies:
  1. /v1/games/list with sortField=Updated (trending)
  2. /v1/games/list with sortField=Visits (popular)
  3. /v1/games/recommendations from seed games
  4. /v1/games/list with sortField=Created (new)
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from config import SEARCH_LIMIT_PER_KEYWORD

logger = logging.getLogger("roblox_search")

# Roblox API base
_GAMES_API = "https://games.roblox.com/v1"

# Default keyword rotation used when no user query is provided.
# Covers major Roblox genres/categories.
DEFAULT_KEYWORDS = [
    "action", "adventure", "fighting", "horror", "comedy",
    "simulator", "tycoon", "rpg", "puzzle", "racing",
    "survival", "roleplay", "obby", "fps", "shooter",
    "anime", "fantasy", "strategy", "sports", "battle",
    # Niche rising keywords
    "dungeon", "crafting", "parkour", "escape", "mystery",
    "idle", "clicker", "platformer", "naval", "medieval",
    "superhero", "zombie", "ninja", "pirate", "space",
]

# Sort options supported by Roblox list endpoint
SORT_NEW = 0       # Created (newest first)
SORT_UPDATED = 1   # Updated (recently updated first)
SORT_POPULAR = 2   # Visits (most visited first)


async def _fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> Optional[dict]:
    """Fetch JSON from a URL with timeout. Returns None on failure."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 429:
                logger.warning("Rate limited on %s, waiting...", url)
                await asyncio.sleep(2)
            else:
                logger.debug("HTTP %d for %s", resp.status, url)
    except asyncio.TimeoutError:
        logger.debug("Timeout fetching %s", url)
    except Exception as exc:
        logger.debug("Error fetching %s: %s", url, exc)
    return None


async def search_keyword(
    session: aiohttp.ClientSession,
    keyword: str,
    limit: int = SEARCH_LIMIT_PER_KEYWORD,
    sort_order: int = SORT_POPULAR,
) -> list[dict]:
    """
    Search Roblox games by keyword.

    Uses /v1/games/list with keyword, limit, and sort parameters.
    Falls back to /v1/games/list with keyword but no sort if the sorted
    variant fails.

    Returns a list of game dicts with at minimum {"id": universe_id}.
    """
    url = (
        f"{_GAMES_API}/games/list"
        f"?model.keyword={_safe_keyword(keyword)}"
        f"&model.limit={min(limit, 30)}"
        f"&model.sortOrder=1"  # Descending
        f"&model.sortField={sort_order}"
    )

    data = await _fetch_json(session, url)
    if data and data.get("data"):
        return _normalize_results(data["data"])

    # Fallback: try without sort (some Roblox deployments don't support sort)
    fallback_url = (
        f"{_GAMES_API}/games/list"
        f"?model.keyword={_safe_keyword(keyword)}"
        f"&model.limit={min(limit, 30)}"
    )
    data = await _fetch_json(session, fallback_url)
    if data and data.get("data"):
        return _normalize_results(data["data"])

    return []


async def search_new_games(
    session: aiohttp.ClientSession,
    limit: int = 30,
) -> list[dict]:
    """
    Fetch most recently created games (new releases).
    Uses sortField=Created (SORT_NEW = 0).
    """
    url = (
        f"{_GAMES_API}/games/list"
        f"?model.limit={min(limit, 30)}"
        f"&model.sortOrder=1"
        f"&model.sortField={SORT_NEW}"
    )
    data = await _fetch_json(session, url)
    if data and data.get("data"):
        return _normalize_results(data["data"])
    return []


async def fetch_recommendations(
    session: aiohttp.ClientSession,
    seed_ids: list[int],
    limit: int = 30,
) -> list[dict]:
    """
    Fetch game recommendations based on seed universe IDs.

    POST /v1/games/recommendations?model.universeIds={ids}
    """
    if not seed_ids:
        return []

    url = (
        f"{_GAMES_API}/games/recommendations"
        f"?model.universeIds={','.join(str(i) for i in seed_ids[:10])}"
    )
    data = await _fetch_json(session, url)
    if data and data.get("data"):
        return _normalize_results(data["data"])
    return []


async def multi_keyword_search(
    session: aiohttp.ClientSession,
    keywords: list[str],
    limit_per_keyword: int = 30,
    max_total: int = 150,
    sort_order: int = SORT_POPULAR,
) -> list[dict]:
    """
    Search multiple keywords concurrently and deduplicate results.

    Returns up to `max_total` unique games.
    """
    if not keywords:
        return []

    tasks = [
        search_keyword(session, kw, limit=limit_per_keyword, sort_order=sort_order)
        for kw in keywords
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen_ids = set()
    merged = []
    for res in results:
        if isinstance(res, list):
            for game in res:
                gid = game.get("id")
                if gid and gid not in seen_ids:
                    seen_ids.add(gid)
                    merged.append(game)
        elif isinstance(res, Exception):
            logger.warning("Keyword search failed: %s", res)

    return merged[:max_total]


def _safe_keyword(keyword: str) -> str:
    """URL-encode a keyword for Roblox API."""
    from urllib.parse import quote
    return quote(keyword.strip(), safe="")


def _normalize_results(data: list[dict]) -> list[dict]:
    """Normalize Roblox API search results to our standard format."""
    games = []
    for item in data:
        uid = item.get("universeId") or item.get("id")
        if not uid:
            continue
        games.append({
            "id": int(uid),
            "name": item.get("name", "Unknown"),
            "playing": item.get("playing", 0),
            "visits": item.get("visits", 0),
            "favorites": item.get("favoritedCount", 0),
            "creator": (item.get("creator") or {}).get("name", "Unknown"),
            "place_id": item.get("rootPlaceId", uid),
            "created": item.get("created"),
            "updated": item.get("updated"),
            "source": "RobloxSearch",
        })
    return games