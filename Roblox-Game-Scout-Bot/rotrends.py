"""
Third-party trending games scraper.

Scrapes ro trends.com and other external sources for trending Roblox games.
Uses pagination and multiple sources to maximize coverage.
"""

import asyncio
import logging
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

from config import ENABLE_ROTRENDS, ENABLE_ROBLOPOLIS

logger = logging.getLogger("rotrends")

# Default User-Agent header
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


async def get_rotrends_games(
    session: aiohttp.ClientSession,
    max_pages: int = 3,
) -> list[dict]:
    """
    Fetch trending Roblox games from RoTrends.com with pagination.

    Extracts game links, names, and Universe IDs from each page.
    Scrapes up to `max_pages` pages (default 3, ~60 games).
    """
    if not ENABLE_ROTRENDS:
        logger.info("RoTrends scraper disabled by config")
        return []

    games = []
    seen_ids = set()

    for page_num in range(1, max_pages + 1):
        url = f"https://rotrends.com"
        if page_num > 1:
            url = f"https://rotrends.com?page={page_num}"

        try:
            async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.debug("RoTrends page %d returned HTTP %d", page_num, resp.status)
                    if page_num == 1:
                        continue  # try next page only if first page failed
                    break

                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                links = soup.find_all("a", href=True)

                page_games = 0
                for link in links:
                    href = link["href"]
                    if "/game/" not in href:
                        continue

                    name = link.text.strip()
                    if not name:
                        continue

                    # Extract Universe ID from URLs like /game/123456/Game-Name
                    parts = href.split("/")
                    game_id = None
                    for part in parts:
                        if part.isdigit():
                            game_id = int(part)
                            break

                    if game_id is None or game_id in seen_ids:
                        continue

                    seen_ids.add(game_id)
                    page_games += 1
                    games.append({
                        "id": game_id,
                        "name": name,
                        "source": "RoTrends",
                        "url": href,
                    })

                logger.debug("RoTrends page %d: %d games found", page_num, page_games)

                # If this page had no games, we've hit the end
                if page_games == 0:
                    break

                # Small delay between pages
                await asyncio.sleep(0.5)

        except asyncio.TimeoutError:
            logger.warning("RoTrends timeout on page %d", page_num)
            break
        except Exception as exc:
            logger.warning("RoTrends error on page %d: %s", page_num, exc)
            break

    logger.info("RoTrends scraper: %d unique games found", len(games))
    return games


async def get_roblopolis_games(
    session: aiohttp.ClientSession,
    max_pages: int = 2,
) -> list[dict]:
    """
    Fetch trending Roblox games from robloxgames.com (previously Roblopolis).

    Another source for trending Roblox games outside of the official API.
    """
    if not ENABLE_ROBLOPOLIS:
        logger.info("Roblopolis scraper disabled by config")
        return []

    games = []
    seen_ids = set()

    base_urls = [
        "https://www.robloxgames.com/trending",
    ]

    for base_url in base_urls:
        for page_num in range(1, max_pages + 1):
            url = base_url if page_num == 1 else f"{base_url}?page={page_num}"

            try:
                async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.debug("RobloxGames page %d returned HTTP %d", page_num, resp.status)
                        continue

                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")

                    # Look for game links and cards
                    page_games = 0
                    for link in soup.find_all("a", href=True):
                        href = link["href"]
                        # Extract universe ID from various URL patterns
                        game_id = _extract_game_id(href)
                        if game_id is None or game_id in seen_ids:
                            continue

                        seen_ids.add(game_id)
                        name = link.text.strip() or link.get("title", "") or f"Game {game_id}"
                        page_games += 1
                        games.append({
                            "id": game_id,
                            "name": name,
                            "source": "RobloxGames",
                            "url": href,
                        })

                    logger.debug("RobloxGames page %d: %d games found", page_num, page_games)
                    if page_games == 0:
                        break
                    await asyncio.sleep(0.5)

            except asyncio.TimeoutError:
                logger.debug("RobloxGames timeout on page %d", page_num)
                break
            except Exception as exc:
                logger.debug("RobloxGames error on page %d: %s", page_num, exc)
                break

    logger.info("RobloxGames scraper: %d unique games found", len(games))
    return games


async def get_third_party_trending(
    session: aiohttp.ClientSession,
) -> list[dict]:
    """
    Aggregate trending games from all third-party sources concurrently.

    Returns deduplicated list of games.
    """
    rotrends_task = get_rotrends_games(session)
    roblopolis_task = get_roblopolis_games(session)

    results = await asyncio.gather(
        rotrends_task,
        roblopolis_task,
        return_exceptions=True,
    )

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
            logger.warning("Third-party scrape failed: %s", res)

    return merged


def _extract_game_id(href: str) -> Optional[int]:
    """Extract a Roblox universe ID from various URL patterns."""
    import re

    # /game/123456/Game-Name
    match = re.search(r"/game/(\d+)", href)
    if match:
        return int(match.group(1))

    # /games/123456
    match = re.search(r"/games/(\d+)", href)
    if match:
        return int(match.group(1))

    # gameid=123456
    match = re.search(r"[?&]gameid=(\d+)", href)
    if match:
        return int(match.group(1))

    # id=123456
    match = re.search(r"[?&]id=(\d+)", href)
    if match:
        return int(match.group(1))

    return None


def get_trending_games():
    """Backward-compatible synchronous alias for get_rotrends_games."""
    # This is a sync wrapper - for async usage, call get_rotrends_games directly
    import requests
    try:
        resp = requests.get(
            "https://rotrends.com",
            headers=_HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        games = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/game/" not in href:
                continue
            name = link.text.strip()
            if not name:
                continue
            parts = href.split("/")
            game_id = None
            for part in parts:
                if part.isdigit():
                    game_id = int(part)
                    break
            if game_id is None:
                continue
            games.append({
                "id": game_id,
                "name": name,
                "source": "RoTrends",
                "url": href,
            })
        return games[:20]
    except Exception:
        return []