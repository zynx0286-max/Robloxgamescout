"""
Trending game discovery sources.

Replaces the old RoTrends/RobloxGames HTML scrapers with the official
**Roblox Charts** data (apis.roblox.com/explore-api — the same endpoints
that power https://www.roblox.com/charts) plus per-game analytics links to
RoMonitor Stats and Creator Exchange for enrichment.

The old rotrends.py depended on scraping third-party HTML layouts, which
broke whenever those sites changed. The explore API is a stable, official
JSON endpoint that is far more reliable.

Supported charts sorts (all feed roblox.com/charts):
  - Top_Trending_V6              "Top Trending"
  - Up_And_Coming_V6             "Up-and-Coming"  (published <28d, user growth)
  - CCU_Based_V1                 "Top Playing Now" (live concurrent players)
  - Fun_With_Friends_V4          "Fun with Friends"
  - Top_Revisited_Existing_Users_V4 "Top Revisited"
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from config import (
    ENABLE_ROBLOX_CHARTS,
    ENABLE_ROMONITOR,
    ENABLE_CREATOR_EXCHANGE,
)

logger = logging.getLogger("trending_sources")

_EXPLORE_API = "https://apis.roblox.com/explore-api/v1"

# Official chart sorts used on roblox.com/charts.
CHART_SORTS = [
    ("Top_Trending_V6", "Top Trending"),
    ("Up_And_Coming_V6", "Up-and-Coming"),
    ("CCU_Based_V1", "Top Playing Now"),
    ("Fun_With_Friends_V4", "Fun with Friends"),
    ("Top_Revisited_Existing_Users_V4", "Top Revisited"),
]

# Random-but-stable session ID required by the explore API.
_SESSION_ID = "4d7b0c2e-6b1f-4e0a-9c3d-8a1f2b4c5d6e"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def build_analytics_links(universe_id: Optional[int]) -> dict:
    """
    Build per-game analytics links for a universe ID.

    Returns a dict of {platform_name: url} that embed builders use for the
    "Market Links" row. Falls back to empty dict when no ID is available.
    """
    if not universe_id:
        return {}

    links = {}
    if ENABLE_ROBLOX_CHARTS:
        # Official Roblox charts page for this game's genre/sort area.
        links["Roblox Charts"] = f"https://www.roblox.com/charts#/genre/any"
    if ENABLE_ROMONITOR:
        links["RoMonitor"] = f"https://romonitorstats.com/experience/{universe_id}"
    if ENABLE_CREATOR_EXCHANGE:
        links["Creator Exchange"] = f"https://creatorexchange.io/game/{universe_id}"
    return links


def get_game_links(universe_id: Optional[int]) -> dict:
    """Backward-compatible alias for build_analytics_links."""
    return build_analytics_links(universe_id)


async def get_roblox_charts_games(
    session: aiohttp.ClientSession,
    max_per_sort: int = 30,
    sorts: Optional[list[tuple[str, str]]] = None,
) -> list[dict]:
    """
    Fetch trending Roblox games from the official Roblox Charts API.

    Uses the explore-api sort endpoints that power roblox.com/charts.
    Each returned game carries its universe ID, live player count, votes,
    genre, and the chart sort it was found under.

    Args:
        session: aiohttp ClientSession.
        max_per_sort: cap of games to keep per chart sort (API returns ~90).
        sorts: list of (sort_id, display_name). Defaults to all CHART_SORTS.

    Returns:
        Deduplicated list of game stubs with at minimum {"id": universe_id}.
    """
    if not ENABLE_ROBLOX_CHARTS:
        logger.info("Roblox Charts source disabled by config")
        return []

    if sorts is None:
        sorts = CHART_SORTS

    tasks = [
        _fetch_sort(session, sort_id, label, max_per_sort)
        for sort_id, label in sorts
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen_ids = set()
    games = []
    for res in results:
        if isinstance(res, list):
            for game in res:
                gid = game.get("id")
                if gid and gid not in seen_ids:
                    seen_ids.add(gid)
                    games.append(game)
        elif isinstance(res, Exception):
            logger.warning("Roblox Charts sort fetch failed: %s", res)

    logger.info("Roblox Charts: %d unique games found", len(games))
    return games


async def _fetch_sort(
    session: aiohttp.ClientSession,
    sort_id: str,
    label: str,
    max_games: int,
) -> list[dict]:
    """Fetch a single chart sort and normalize its games."""
    url = (
        f"{_EXPLORE_API}/get-sort-content"
        f"?sessionId={_SESSION_ID}"
        f"&sortId={sort_id}"
    )
    games = []
    try:
        async with session.get(
            url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.debug(
                    "Roblox Charts sort %s returned HTTP %d", sort_id, resp.status
                )
                return games
            data = await resp.json()
            for item in (data.get("games") or [])[:max_games]:
                uid = item.get("universeId") or item.get("id")
                if not uid:
                    continue
                games.append({
                    "id": int(uid),
                    "name": item.get("name", "Unknown"),
                    "playing": item.get("playerCount", 0),
                    "up_votes": item.get("totalUpVotes", 0),
                    "down_votes": item.get("totalDownVotes", 0),
                    "genre": item.get("genreL1", ""),
                    "place_id": item.get("rootPlaceId", uid),
                    "source": "RobloxCharts",
                    "charts_sort": label,
                })
            logger.debug("Roblox Charts sort %s: %d games", sort_id, len(games))
    except asyncio.TimeoutError:
        logger.warning("Roblox Charts timeout on sort %s", sort_id)
    except Exception as exc:
        logger.warning("Roblox Charts error on sort %s: %s", sort_id, exc)
    return games


async def get_romonitor_games(
    session: aiohttp.ClientSession,
    max_pages: int = 1,
) -> list[dict]:
    """
    Best-effort RoMonitor Stats discovery source.

    RoMonitor Stats (romonitorstats.com) serves its charts behind Cloudflare
    and does not expose a public game-list API, so direct scraping is usually
    blocked. This source is intentionally optional and always degrades
    gracefully to an empty list. Per-game analytics links are still attached
    to embeds via build_analytics_links().
    """
    if not ENABLE_ROMONITOR:
        logger.info("RoMonitor source disabled by config")
        return []
    logger.info("RoMonitor Stats has no public game-list API; skipping scrape")
    return []


async def get_creator_exchange_games(
    session: aiohttp.ClientSession,
    max_pages: int = 1,
) -> list[dict]:
    """
    Best-effort Creator Exchange discovery source.

    Creator Exchange (creatorexchange.io) is a client-rendered app with no
    public game-list endpoint, so this source is optional and degrades to an
    empty list. Per-game pages are linked from embeds via
    build_analytics_links().
    """
    if not ENABLE_CREATOR_EXCHANGE:
        logger.info("Creator Exchange source disabled by config")
        return []
    logger.info("Creator Exchange has no public game-list API; skipping scrape")
    return []


async def get_third_party_trending(
    session: aiohttp.ClientSession,
) -> list[dict]:
    """
    Aggregate trending games from all enabled third-party sources.

    Returns a deduplicated list of game stubs.
    """
    tasks = [
        get_roblox_charts_games(session),
        get_romonitor_games(session),
        get_creator_exchange_games(session),
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
            logger.warning("Third-party trending fetch failed: %s", res)

    return merged


def get_trending_games() -> list[dict]:
    """Backward-compatible synchronous alias used by the legacy scanner path."""
    import requests

    if not ENABLE_ROBLOX_CHARTS:
        return []

    games = []
    seen_ids = set()
    for sort_id, label in CHART_SORTS:
        try:
            resp = requests.get(
                f"{_EXPLORE_API}/get-sort-content"
                f"?sessionId={_SESSION_ID}&sortId={sort_id}",
                headers=_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in (data.get("games") or [])[:30]:
                uid = item.get("universeId") or item.get("id")
                if not uid or uid in seen_ids:
                    continue
                seen_ids.add(uid)
                games.append({
                    "id": int(uid),
                    "name": item.get("name", "Unknown"),
                    "source": "RobloxCharts",
                    "charts_sort": label,
                })
        except Exception as exc:
            logger.warning("Sync Roblox Charts fetch failed for %s: %s", sort_id, exc)
    return games
