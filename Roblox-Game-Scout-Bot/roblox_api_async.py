"""
Async Roblox API client with concurrent fetching and batch support.

Provides:
  - Batch universe ID queries (up to 100 IDs per request)
  - Concurrent detail fetching with rate-limit awareness
  - Creator/group info retrieval
  - Game ratings, social links, thumbnails
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from config import MAX_CONCURRENT_FETCHES

logger = logging.getLogger("roblox_api_async")

_GAMES_API = "https://games.roblox.com/v1"
_GROUPS_API = "https://groups.roblox.com/v1"
_THUMBNAILS_API = "https://thumbnails.roblox.com/v1"

# Max universe IDs per batch request (Roblox API limit)
_BATCH_SIZE = 100

# Semaphore to cap concurrent requests and avoid rate-limiting
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    return _semaphore


async def fetch_game_details_batch(
    session: aiohttp.ClientSession,
    universe_ids: list[int],
) -> list[Optional[dict]]:
    """
    Fetch game details for multiple universe IDs in one batch request.

    GET /v1/games?universeIds={id1},{id2},...

    Returns a list of normalized game dicts in the same order as the input.
    Missing/unresolvable IDs return None.
    """
    if not universe_ids:
        return []

    # Batch into chunks of 100
    batches = [universe_ids[i:i + _BATCH_SIZE] for i in range(0, len(universe_ids), _BATCH_SIZE)]
    results: list[Optional[dict]] = []

    async def _fetch_batch(batch_ids: list[int]) -> dict[int, dict]:
        url = f"{_GAMES_API}/games?universeIds={','.join(str(uid) for uid in batch_ids)}"
        async with _get_semaphore():
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return _normalize_batch(data.get("data", []), batch_ids)
                    if resp.status == 429:
                        logger.warning("Rate limited on batch fetch, waiting...")
                        await asyncio.sleep(2)
                    else:
                        logger.debug("Batch fetch returned HTTP %d", resp.status)
            except asyncio.TimeoutError:
                logger.debug("Batch fetch timed out")
            except Exception as exc:
                logger.debug("Batch fetch error: %s", exc)
        return {}

    # Fetch all batches concurrently
    batch_tasks = [_fetch_batch(batch) for batch in batches]
    batch_maps = await asyncio.gather(*batch_tasks, return_exceptions=True)

    # Merge results maintaining input order
    id_to_game: dict[int, dict] = {}
    for bm in batch_maps:
        if isinstance(bm, dict):
            id_to_game.update(bm)
        elif isinstance(bm, Exception):
            logger.warning("Batch fetch exception: %s", bm)

    for uid in universe_ids:
        results.append(id_to_game.get(uid))

    return results


async def fetch_game_details(
    session: aiohttp.ClientSession,
    universe_id: int,
) -> Optional[dict]:
    """
    Fetch details for a single universe ID.

    Useful when you only need one game, but prefer batch for bulk.
    """
    results = await fetch_game_details_batch(session, [universe_id])
    return results[0] if results else None


async def fetch_game_rating(
    session: aiohttp.ClientSession,
    universe_id: int,
) -> Optional[dict]:
    """
    Fetch rating data for a game.

    GET /v1/games/votes?universeIds={id}

    Returns dict with upVotes, downVotes, ratio, or None.
    """
    url = f"{_GAMES_API}/games/votes?universeIds={universe_id}"
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("data", []):
                        up = item.get("upVotes", 0)
                        down = item.get("downVotes", 0)
                        total = up + down
                        ratio = (up / total * 100.0) if total > 0 else 0.0
                        return {
                            "up_votes": up,
                            "down_votes": down,
                            "total_votes": total,
                            "rating_percent": round(ratio, 1),
                        }
        except asyncio.TimeoutError:
            logger.debug("Rating fetch timeout for universe %s", universe_id)
        except Exception as exc:
            logger.debug("Rating fetch error for universe %s: %s", universe_id, exc)
    return None


async def fetch_game_ratings_batch(
    session: aiohttp.ClientSession,
    universe_ids: list[int],
) -> dict[int, dict]:
    """
    Fetch ratings for multiple games in a single batch request.

    GET /v1/games/votes?universeIds={id1},{id2},...

    Returns {universe_id: rating_dict} for ratings that were found.
    """
    if not universe_ids:
        return {}

    url = f"{_GAMES_API}/games/votes?universeIds={','.join(str(uid) for uid in universe_ids)}"
    result_map: dict[int, dict] = {}

    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("data", []):
                        uid = item.get("universeId") or item.get("id")
                        if not uid:
                            continue
                        uid = int(uid)
                        up = item.get("upVotes", 0)
                        down = item.get("downVotes", 0)
                        total = up + down
                        ratio = (up / total * 100.0) if total > 0 else 0.0
                        result_map[uid] = {
                            "up_votes": up,
                            "down_votes": down,
                            "total_votes": total,
                            "rating_percent": round(ratio, 1),
                        }
        except Exception as exc:
            logger.debug("Batch ratings fetch error: %s", exc)

    return result_map


async def fetch_game_thumbnail(
    session: aiohttp.ClientSession,
    universe_id: int,
    size: str = "512x512",
    format: str = "Png",
) -> Optional[str]:
    """
    Fetch the thumbnail URL for a game.

    GET /v1/games/{universeId}/icon?size={size}&format={format}
    Also tried: thumbnails.roblox.com/v1/games/icons?universeIds={id}&size={size}&format=Png
    """
    # Try the games API icon endpoint first
    url = f"{_GAMES_API}/games/{universe_id}/icon"
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data"):
                        return data["data"][0].get("imageUrl")
        except Exception:
            pass

    # Fallback to thumbnails API
    url2 = (
        f"{_THUMBNAILS_API}/games/icons"
        f"?universeIds={universe_id}"
        f"&size={size}"
        f"&format=Png"
        f"&isCircular=false"
    )
    async with _get_semaphore():
        try:
            async with session.get(url2, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("data", []):
                        url_val = item.get("imageUrl")
                        if url_val:
                            return url_val
        except Exception:
            pass

    return None


async def fetch_game_description(
    session: aiohttp.ClientSession,
    universe_id: int,
) -> Optional[str]:
    """
    Fetch the description text for a game.

    Uses /v1/games/multigame-details which returns game description.
    """
    url = f"{_GAMES_API}/games/multigame-details?universeIds={universe_id}"
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for g in data.get("data", []):
                        desc = g.get("description", "")
                        if desc:
                            return desc
        except Exception as exc:
            logger.debug("Description fetch error for universe %s: %s", universe_id, exc)
    return None


async def fetch_creator_details(
    session: aiohttp.ClientSession,
    universe_id: int,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Fetch creator ID, type (User/Group), and name for a universe.

    Uses /v1/games/multigame-details endpoint.
    Returns (creator_id, creator_type, creator_name) or (None, None, None).
    """
    url = f"{_GAMES_API}/games/multigame-details?universeIds={universe_id}"
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for g in data.get("data", []):
                        creator = g.get("creator", {})
                        if creator.get("id"):
                            return (
                                int(creator["id"]),
                                creator.get("type", "User"),
                                creator.get("name", ""),
                            )
        except Exception as exc:
            logger.debug("Creator fetch error: %s", exc)
    return None, None, None


async def fetch_group_size(
    session: aiohttp.ClientSession,
    group_id: int,
) -> int:
    """Fetch the member count for a Roblox group."""
    url = f"{_GROUPS_API}/groups/{group_id}"
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(data.get("memberCount", 0))
        except Exception as exc:
            logger.debug("Group size fetch error: %s", exc)
    return 0


async def fetch_user_past_games(
    session: aiohttp.ClientSession,
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    """Fetch past games created by a specific Roblox user."""
    url = (
        f"https://games.roblox.com/v2/users/{user_id}/games"
        f"?sortOrder=Desc&limit={limit}"
    )
    async with _get_semaphore():
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
        except Exception as exc:
            logger.debug("User past games fetch error: %s", exc)
    return []


async def enrich_game_with_ratings(
    session: aiohttp.ClientSession,
    game: dict,
) -> dict:
    """
    Enrich a game dict with rating data.

    Fetches rating and attaches rating_percent, up_votes, down_votes.
    Returns the modified game dict.
    """
    uid = game.get("id")
    if not uid:
        return game

    rating = await fetch_game_rating(session, uid)
    if rating:
        game["rating_percent"] = rating["rating_percent"]
        game["up_votes"] = rating["up_votes"]
        game["down_votes"] = rating["down_votes"]
        game["total_votes"] = rating["total_votes"]
    else:
        game["rating_percent"] = 0.0

    return game


async def enrich_game_with_thumbnail(
    session: aiohttp.ClientSession,
    game: dict,
) -> dict:
    """
    Enrich a game dict with a thumbnail URL.
    """
    uid = game.get("id")
    if not uid:
        return game

    thumb_url = await fetch_game_thumbnail(session, uid)
    if thumb_url:
        game["thumbnail_url"] = thumb_url

    return game


async def enrich_game_with_description(
    session: aiohttp.ClientSession,
    game: dict,
) -> dict:
    """
    Enrich a game dict with its description text.
    """
    uid = game.get("id")
    if not uid:
        return game

    desc = await fetch_game_description(session, uid)
    if desc:
        game["description"] = desc

    return game


def _normalize_batch(
    data: list[dict],
    requested_ids: list[int],
) -> dict[int, dict]:
    """
    Normalize Roblox API batch response to {universe_id: game_dict}.

    Only includes games that were present in the response.
    """
    result = {}
    for item in data:
        uid = item.get("universeId") or item.get("id")
        if not uid:
            continue
        uid = int(uid)
        result[uid] = {
            "id": uid,
            "place_id": item.get("rootPlaceId", uid),
            "name": item.get("name", "Unknown"),
            "playing": item.get("playing", 0),
            "visits": item.get("visits", 0),
            "favorites": item.get("favoritedCount", 0),
            "creator": (item.get("creator") or {}).get("name", "Unknown"),
            "created": item.get("created"),
            "updated": item.get("updated"),
            "genre": _extract_genre(item),
        }
    return result


def _extract_genre(item: dict) -> str:
    """Extract genre string from a Roblox API game item."""
    genre = item.get("genre") or item.get("genreName") or ""
    if isinstance(genre, dict):
        return genre.get("name", "")
    return str(genre)