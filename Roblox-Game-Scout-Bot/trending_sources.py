"""
Roblox Charts discovery.

Trending discovery is the official **Roblox Charts** API
(apis.roblox.com/explore-api — the same endpoints that power
https://www.roblox.com/charts). That is the entire source list: the former
third-party integrations were removed because none of them expose a stable,
public game-list or metric API.

What was removed and why (verified 2026-10):
  - **RoMonitor Stats** — charts sit behind Cloudflare and there is no public
    game-list endpoint. The old function always returned an empty list.
  - **Creator Exchange** — client-rendered app, no public game-list endpoint.
    Also always returned empty.
  - **BloxAtlas / Atlas** — Atlas was the only third party actually returning
    data (7 days of history). Growth history is now computed from the bot's
    own `game_history` snapshots over 30 days — deeper, and with no external
    dependency. See `history.py`.

A browser-like User-Agent is still sent because Roblox's public web APIs 403
requests that identify as a bare script client. That is a header, not a
dependency: nothing here leaves Roblox's own infrastructure.

Supported chart sorts (all feed roblox.com/charts):
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

from config import ENABLE_ROBLOX_CHARTS

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
    """Per-game links for the embed's market-links row.

    Roblox-native only: the game's own page. (Charts is a browsing surface,
    not a per-game URL, so it is not a link worth attaching to every embed.)
    Returns {} when no ID is available.
    """
    if not universe_id:
        return {}
    return {"Roblox": f"https://www.roblox.com/games/{int(universe_id)}"}


def get_game_links(universe_id: Optional[int]) -> dict:
    """Backward-compatible alias for build_analytics_links."""
    return build_analytics_links(universe_id)


async def get_roblox_charts_games(
    session: aiohttp.ClientSession,
    max_per_sort: int = 30,
    sorts: Optional[list[tuple[str, str]]] = None,
) -> list[dict]:
    """Fetch trending games from the official Roblox Charts API.

    Uses the explore-api sort endpoints that power roblox.com/charts. Each
    returned game carries its universe ID, live player count, votes, genre,
    and the chart sort it was found under.

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

    # Merge across sorts, keeping every chart a game appears on: the first
    # stub for an id wins the base fields, and each additional sort is
    # appended to its chart_appearances list. (A game at #3 on Top Trending
    # AND #80 on Top Playing Now is a #3 game — but it is on both charts,
    # and chart_rank.py records every position.)
    games = []
    by_id = {}
    for res in results:
        if isinstance(res, list):
            for game in res:
                gid = game.get("id")
                if not gid:
                    continue
                appearance = (
                    game.get("charts_sort_id") or "",
                    game.get("charts_sort") or "",
                    game.get("chart_rank"),
                )
                existing = by_id.get(gid)
                if existing is None:
                    game["chart_appearances"] = [appearance]
                    by_id[gid] = game
                    games.append(game)
                elif appearance not in existing["chart_appearances"]:
                    existing["chart_appearances"].append(appearance)
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
                # A non-200 here almost always means Roblox retired or
                # renamed a sort id (their 400 body is literally "Sort not
                # found or not enabled"). That shrinks discovery AND the
                # chart-rank signal silently, so it must be visible at
                # warning level — a debug line was how a dead sort would
                # have gone unnoticed for months.
                body = ""
                try:
                    body = (await resp.text())[:120]
                except Exception:
                    pass
                logger.warning(
                    "Roblox Charts sort %s returned HTTP %d (%s) — sort may "
                    "have been retired or renamed; discovery and chart "
                    "ranks for it are now empty.",
                    sort_id,
                    resp.status,
                    body or "no body",
                )
                return games
            data = await resp.json()
            # position = the game's rank within this sort (1 = top of the
            # chart). enumerate() over the API's ordering, not the truncated
            # slice, so the rank is the true chart position.
            for position, item in enumerate(data.get("games") or [], start=1):
                if position > max_games:
                    break
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
                    "charts_sort_id": sort_id,
                    "chart_rank": position,
                })
            logger.debug("Roblox Charts sort %s: %d games", sort_id, len(games))
    except asyncio.TimeoutError:
        logger.warning("Roblox Charts timeout on sort %s", sort_id)
    except Exception as exc:
        logger.warning("Roblox Charts error on sort %s: %s", sort_id, exc)
    return games


async def get_charts_trending(session: aiohttp.ClientSession) -> list[dict]:
    """Trending games from Roblox Charts (the sole trending source)."""
    return await get_roblox_charts_games(session)


def get_trending_games() -> list[dict]:
    """Backward-compatible synchronous alias used by the legacy scanner path."""
    import requests

    if not ENABLE_ROBLOX_CHARTS:
        return []

    games = []
    by_id = {}
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
            for position, item in enumerate(data.get("games") or [], start=1):
                if position > 30:
                    break
                uid = item.get("universeId") or item.get("id")
                if not uid:
                    continue
                appearance = (sort_id, label, position)
                existing = by_id.get(uid)
                if existing is None:
                    game = {
                        "id": int(uid),
                        "name": item.get("name", "Unknown"),
                        "source": "RobloxCharts",
                        "charts_sort": label,
                        "charts_sort_id": sort_id,
                        "chart_rank": position,
                        "chart_appearances": [appearance],
                    }
                    by_id[uid] = game
                    games.append(game)
                elif appearance not in existing["chart_appearances"]:
                    existing["chart_appearances"].append(appearance)
        except Exception as exc:
            logger.warning("Sync Roblox Charts fetch failed for %s: %s", sort_id, exc)
    return games
