"""
Roblox Search API client.

Keyword-based game discovery.

IMPORTANT (2026-09): the legacy discovery endpoints
`/v1/games/list` (keyword/new/updated sorts) and `/v1/games/recommendations`
were retired by Roblox — both now return 404 for unauthenticated callers
(verified live). Discovery is therefore rebuilt on the official search API
that powers roblox.com/search:

    GET https://apis.roblox.com/search-api/omni-search
        ?searchQuery={keyword}&pageType=all&sessionId={uuid}

The sessionId is mandatory (any stable UUID) — without it the API returns
HTTP 200 with empty results. Each hit carries universeId, name, playerCount,
rootPlaceId, votes, and creator name, so one request fully enriches stubs.

"New games" discovery uses a "new" keyword query because the search API
exposes no sort parameter. Recommendations are approximated by searching
for the seed game's name and excluding the seed itself.
"""

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import aiohttp

from config import SEARCH_LIMIT_PER_KEYWORD

logger = logging.getLogger("roblox_search")

# Official search API (see module docstring for why games/list is gone).
_SEARCH_API = "https://apis.roblox.com/search-api/omni-search"
_GAMES_API = "https://games.roblox.com/v1"

# Random-but-stable session ID required by the search API.
_SESSION_ID = "4d7b0c2e-6b1f-4e0a-9c3d-8a1f2b4c5d6e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

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


async def _fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> Optional[dict]:
    """Fetch JSON from a URL with timeout. Returns None on failure."""
    try:
        async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
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


def _normalize_omni_results(data: dict, limit: int) -> list[dict]:
    """Flatten an omni-search payload into normalized game stubs.

    The payload nests game hits under searchResults[*].contents[]; only the
    "Game" content group is relevant.
    """
    games = []
    for group in data.get("searchResults") or []:
        if group.get("contentGroupType") != "Game":
            continue
        for item in group.get("contents") or []:
            uid = item.get("universeId")
            if not uid:
                continue
            games.append({
                "id": int(uid),
                "name": item.get("name", "Unknown"),
                "playing": item.get("playerCount", 0),
                "visits": 0,
                "favorites": 0,
                "up_votes": item.get("totalUpVotes", 0),
                "down_votes": item.get("totalDownVotes", 0),
                "creator": item.get("creatorName", "Unknown"),
                "place_id": item.get("rootPlaceId") or uid,
                "source": "RobloxSearch",
            })
            if len(games) >= limit:
                return games
    return games


async def search_keyword(
    session: aiohttp.ClientSession,
    keyword: str,
    limit: int = SEARCH_LIMIT_PER_KEYWORD,
    sort_order: int = None,  # unused; kept for call-site compatibility
) -> list[dict]:
    """
    Search Roblox games by keyword via the omni-search API.

    Returns a list of game dicts with at minimum {"id": universe_id}.
    """
    url = (
        f"{_SEARCH_API}"
        f"?searchQuery={quote(keyword.strip(), safe='')}"
        f"&pageType=all"
        f"&sessionId={_SESSION_ID}"
    )

    data = await _fetch_json(session, url)
    if data and data.get("searchResults"):
        return _normalize_omni_results(data, limit=max(limit, 1))
    return []


async def search_new_games(
    session: aiohttp.ClientSession,
    limit: int = 30,
) -> list[dict]:
    """
    Fetch recently-created games.

    The search API exposes no sort parameter (the old sortField=Created
    endpoint is retired), so this queries the "new" keyword as the closest
    available approximation.
    """
    return await search_keyword(session, "new", limit=limit)


async def fetch_recommendations(
    session: aiohttp.ClientSession,
    seed_ids: list[int],
    limit: int = 30,
) -> list[dict]:
    """
    Fetch games similar to the seed games.

    The /v1/games/recommendations endpoint is retired, so similarity is
    approximated: look up the first seed's name, search for it, and exclude
    the seed itself. Returns [] if the seed lookup fails.
    """
    if not seed_ids:
        return []

    try:
        async with session.get(
            f"{_GAMES_API}/games?universeIds={int(seed_ids[0])}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            entries = data.get("data") or []
            seed_name = entries[0].get("name") if entries else None
    except Exception as exc:
        logger.debug("Seed name lookup failed: %s", exc)
        return []

    if not seed_name:
        return []

    results = await search_keyword(session, seed_name, limit=limit + 1)
    return [g for g in results if g.get("id") != int(seed_ids[0])][:limit]


def _merge_game_lists(lists: list, max_total: int) -> list[dict]:
    """Merge search results, deduplicating by game id and capping total.

    Non-list entries (exceptions from asyncio.gather) are logged and skipped.
    """
    seen_ids = set()
    merged = []
    for res in lists:
        if isinstance(res, list):
            for game in res:
                gid = game.get("id")
                if gid and gid not in seen_ids:
                    seen_ids.add(gid)
                    merged.append(game)
        elif isinstance(res, Exception):
            logger.warning("Keyword search failed: %s", res)
    return merged[:max_total]


async def multi_keyword_search(
    session: aiohttp.ClientSession,
    keywords: list[str],
    limit_per_keyword: int = 30,
    max_total: int = 150,
    sort_order: int = None,  # unused; kept for call-site compatibility
) -> list[dict]:
    """
    Search multiple keywords concurrently and deduplicate results.

    Returns up to `max_total` unique games.
    """
    if not keywords:
        return []

    tasks = [
        search_keyword(session, kw, limit=limit_per_keyword)
        for kw in keywords
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return _merge_game_lists(results, max_total)
