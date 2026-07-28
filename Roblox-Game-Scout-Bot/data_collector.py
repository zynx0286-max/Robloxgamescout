"""
Game collection engine.

Orchestrates all discovery sources (Roblox API search, curated seeds,
third-party scrapers) and merges results into a deduplicated list.

Supports both sync (legacy) and async (new) collection modes.
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from game_search import get_curated_seeds
from rotrends import get_third_party_trending
from roblox_search import (
    DEFAULT_KEYWORDS,
    multi_keyword_search,
    search_new_games,
    fetch_recommendations,
    SORT_POPULAR,
    SORT_UPDATED,
)
from config import (
    SEARCH_KEYWORDS_PER_CYCLE,
    SEARCH_TOTAL_LIMIT,
    ENABLE_ROTRENDS,
)
from utils import remove_duplicates

logger = logging.getLogger("data_collector")

# Track which keywords we've used so we rotate through them
_keyword_index = 0


def _get_next_keywords(count: int) -> list[str]:
    """Get the next N keywords from the rotation, cycling through the list."""
    global _keyword_index
    if not DEFAULT_KEYWORDS:
        return []

    keywords = []
    for _ in range(count):
        kw = DEFAULT_KEYWORDS[_keyword_index % len(DEFAULT_KEYWORDS)]
        keywords.append(kw)
        _keyword_index += 1

    return keywords


async def collect_games_async(
    session: aiohttp.ClientSession,
    seed_ids: Optional[list[int]] = None,
    is_deep_scan: bool = False,
) -> list[dict]:
    """
    Collect games from all sources asynchronously.

    Sources (in priority order):
      1. Roblox API keyword search (rotating keywords)
      2. Roblox API new games (recently created)
      3. Roblox API recommendations (from seed games)
      4. Curated seed list (fallback)
      5. Third-party scrapers (RoTrends, RobloxGames)

    Args:
        session: aiohttp ClientSession for HTTP requests
        seed_ids: Optional list of universe IDs for recommendations
        is_deep_scan: If True, use more keywords and sources

    Returns:
        Deduplicated list of game stubs with at minimum {"id": universe_id}
    """
    all_games = []

    # --- Source 1: Roblox API keyword search ---
    keyword_count = SEARCH_KEYWORDS_PER_CYCLE * (2 if is_deep_scan else 1)
    keywords = _get_next_keywords(keyword_count)
    logger.info("Searching keywords: %s", keywords)

    try:
        # Search by popularity (most visited)
        popular_games = await multi_keyword_search(
            session,
            keywords,
            limit_per_keyword=30,
            max_total=SEARCH_TOTAL_LIMIT,
            sort_order=SORT_POPULAR,
        )
        all_games.extend(popular_games)
        logger.info("Keyword search (popular): %d games", len(popular_games))
    except Exception as exc:
        logger.warning("Keyword search (popular) failed: %s", exc)

    # --- Source 2: Recently updated games (trending) ---
    if is_deep_scan:
        try:
            updated_games = await multi_keyword_search(
                session,
                keywords[:3],  # Use first 3 keywords for "recently updated" sort
                limit_per_keyword=30,
                max_total=90,
                sort_order=SORT_UPDATED,
            )
            all_games.extend(updated_games)
            logger.info("Keyword search (updated): %d games", len(updated_games))
        except Exception as exc:
            logger.warning("Keyword search (updated) failed: %s", exc)

    # --- Source 3: Newly created games ---
    try:
        new_games = await search_new_games(session, limit=30)
        all_games.extend(new_games)
        logger.info("New games search: %d games", len(new_games))
    except Exception as exc:
        logger.warning("New games search failed: %s", exc)

    # --- Source 4: Recommendations from seed games ---
    if seed_ids:
        try:
            rec_games = await fetch_recommendations(session, seed_ids[:5], limit=30)
            all_games.extend(rec_games)
            logger.info("Recommendations: %d games", len(rec_games))
        except Exception as exc:
            logger.warning("Recommendations failed: %s", exc)

    # --- Source 5: Curated seed list (always included) ---
    curated = get_curated_seeds()
    all_games.extend(curated)
    logger.info("Curated seeds: %d games", len(curated))

    # --- Source 6: Third-party scrapers ---
    if ENABLE_ROTRENDS:
        try:
            third_party = await get_third_party_trending(session)
            all_games.extend(third_party)
            logger.info("Third-party scrapers: %d games", len(third_party))
        except Exception as exc:
            logger.warning("Third-party scrapers failed: %s", exc)

    # Deduplicate and return
    unique = remove_duplicates(all_games)
    logger.info(
        "Total collected: %d unique games (from %d raw)",
        len(unique),
        len(all_games),
    )
    return unique


def collect_games():
    """
    Synchronous wrapper for collect_games_async.

    Used by the legacy synchronous scanner path.
    Falls back to curated seeds + sync rotrends if async is unavailable.
    """
    # Try async path first
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def _run():
                async with aiohttp.ClientSession() as session:
                    return await collect_games_async(session)
            return loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception as exc:
        logger.warning("Async collection failed, falling back to sync: %s", exc)

    # Fallback: curated seeds + sync rotrends
    from rotrends import get_trending_games
    games = get_curated_seeds()
    try:
        games.extend(get_trending_games())
    except Exception as exc:
        logger.warning("Sync rotrends failed: %s", exc)
    return remove_duplicates(games)