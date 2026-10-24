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
from trending_sources import get_charts_trending
import chart_rank
from roblox_search import (
    DEFAULT_KEYWORDS,
    multi_keyword_search,
    search_new_games,
    fetch_recommendations,
)
from config import (
    SEARCH_KEYWORDS_PER_CYCLE,
    SEARCH_TOTAL_LIMIT,
    ENABLE_ROBLOX_CHARTS,
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
      5. Roblox Charts (official explore API)

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
        # Keyword search via the official omni-search API. (The legacy
        # /v1/games/list endpoint was retired by Roblox, so there is no
        # sort order anymore — popularity ranking happens at scoring time.)
        popular_games = await multi_keyword_search(
            session,
            keywords,
            limit_per_keyword=30,
            max_total=SEARCH_TOTAL_LIMIT,
        )
        all_games.extend(popular_games)
        logger.info("Keyword search: %d games", len(popular_games))
    except Exception as exc:
        logger.warning("Keyword search failed: %s", exc)

    # --- Source 2: Trend-flavored keyword queries (deep scans) ---
    if is_deep_scan:
        try:
            trend_games = await multi_keyword_search(
                session,
                ["trending", "popular", "new"],
                limit_per_keyword=30,
                max_total=90,
            )
            all_games.extend(trend_games)
            logger.info("Trend keyword search: %d games", len(trend_games))
        except Exception as exc:
            logger.warning("Trend keyword search failed: %s", exc)

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

    # --- Source 6: Roblox Charts (official explore API) ---
    chart_ids: set[int] = set()
    chart_games: list[dict] = []
    if ENABLE_ROBLOX_CHARTS:
        try:
            chart_games = await get_charts_trending(session)
            all_games.extend(chart_games)
            chart_ids = {g["id"] for g in chart_games if g.get("id")}
            logger.info("Roblox Charts: %d games", len(chart_games))
        except Exception as exc:
            logger.warning("Roblox Charts fetch failed: %s", exc)

    # Deduplicate and return. Chart membership is stamped after the merge:
    # dedupe keeps only one stub per id, so a game found first by keyword
    # search would otherwise lose the fact that it is also on a chart.
    unique = remove_duplicates(all_games)
    # The rank fields live on the chart stub — but dedupe may have kept the
    # keyword-search stub instead. Merge them across explicitly, or an
    # on-charts game reaches the filters with on_charts=True and no rank
    # (the same failure mode the on_charts stamp exists to prevent).
    chart_stub_by_id = {g["id"]: g for g in chart_games if g.get("id")}
    for game in unique:
        gid = game.get("id")
        game["on_charts"] = gid in chart_ids
        chart_stub = chart_stub_by_id.get(gid)
        if chart_stub is not None:
            if chart_stub.get("chart_appearances"):
                game["chart_appearances"] = chart_stub["chart_appearances"]
            if chart_stub.get("chart_rank"):
                game["chart_rank"] = chart_stub["chart_rank"]
            if chart_stub.get("charts_sort"):
                game["charts_sort"] = chart_stub["charts_sort"]
            if chart_stub.get("charts_sort_id"):
                game["charts_sort_id"] = chart_stub["charts_sort_id"]

    # Record this cycle's chart positions before returning. Discovery is the
    # only place that knows every game's position within every sort, and
    # chart_rank.py turns those rows into the rank-delta / best-rank signals
    # used by scoring and embeds. Best-effort: a local SQLite write must
    # never be able to fail a scan.
    try:
        positions = []
        for stub in chart_games:
            for sort_id, label, position in stub.get("chart_appearances") or []:
                if position:
                    positions.append((stub["id"], sort_id, label, position))
        if positions:
            written = chart_rank.record_positions(positions)
            logger.info("Chart positions recorded: %d rows", written)
    except Exception as exc:
        logger.warning("Chart position recording failed: %s", exc)

    logger.info(
        "Total collected: %d unique games (from %d raw, %d on Roblox charts)",
        len(unique),
        len(all_games),
        len(chart_ids),
    )
    return unique


def collect_games():
    """
    Synchronous wrapper for collect_games_async.

    Used by the legacy synchronous scanner path.
    Falls back to curated seeds + sync Roblox Charts if async is unavailable.
    """
    # Try async path first
    try:
        async def _run():
            async with aiohttp.ClientSession() as session:
                return await collect_games_async(session)
        return asyncio.run(_run())
    except Exception as exc:
        logger.warning("Async collection failed, falling back to sync: %s", exc)

    # Fallback: curated seeds + sync Roblox Charts
    from trending_sources import get_trending_games
    games = get_curated_seeds()
    try:
        games.extend(get_trending_games())
    except Exception as exc:
        logger.warning("Sync Roblox Charts failed: %s", exc)
    return remove_duplicates(games)