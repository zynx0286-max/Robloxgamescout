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
import random
import time
from typing import Optional

import aiohttp

from config import (
    MAX_CONCURRENT_FETCHES,
    ROBLOX_API_MAX_RETRIES,
    ROBLOX_API_MIN_INTERVAL,
)

logger = logging.getLogger("roblox_api_async")

_GAMES_API = "https://games.roblox.com/v1"
_GROUPS_API = "https://groups.roblox.com/v1"
_THUMBNAILS_API = "https://thumbnails.roblox.com/v1"

# Max universe IDs per batch request, per endpoint. Verified against the
# live API (2026-10):
#   games.roblox.com/v1/games        -> 50   (returns HTTP 400 from 60 up)
#   games.roblox.com/v1/games/votes  -> 100
#   thumbnails.roblox.com/v1/games/* -> 100
# The details endpoint was previously batched at 100 here, so every full
# chunk was rejected with HTTP 400 and every game inside it vanished from
# the scan without an alert, an error, or anything above a DEBUG log.
_DETAILS_BATCH_SIZE = 50
_BATCH_SIZE = 100

# Semaphore to cap concurrent requests and avoid rate-limiting
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    return _semaphore


class _Pacer:
    """Enforce a minimum gap between requests to one host.

    ``games.roblox.com`` throttles cloud/datacenter IP ranges far harder
    than residential ones (a shared free host can sit at HTTP 429 within
    seconds of boot). Concurrency alone does not prevent that — a host
    that fires 20 parallel requests still burns the whole budget in one
    burst — so every request to that host waits its turn here first.
    """

    def __init__(self, min_interval: float):
        self._min = float(min_interval)
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.waits = 0
        self.max_wait = 0.0

    async def acquire(self) -> float:
        if self._min <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                self.waits += 1
                self.max_wait = max(self.max_wait, wait)
                await asyncio.sleep(wait)
                now = time.monotonic()
            # Jitter so concurrent loops never form a metronome pattern
            # that a rate limiter can lock onto.
            self._next_at = now + self._min + random.uniform(0, self._min * 0.5)
            return wait


_pacer: Optional[_Pacer] = None


def _get_pacer() -> _Pacer:
    global _pacer
    if _pacer is None:
        _pacer = _Pacer(ROBLOX_API_MIN_INTERVAL)
    return _pacer


def reset_rate_limiters() -> None:
    """Drop the cached semaphore/pacer. Tests call this between event loops."""
    global _semaphore, _pacer
    _semaphore = None
    _pacer = None


def _retry_after_seconds(headers) -> float:
    """Parse Retry-After (seconds form only; HTTP-date is rare here)."""
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


async def request_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 15,
    retries: int = ROBLOX_API_MAX_RETRIES,
    pace: bool = True,
) -> tuple[int, Optional[dict]]:
    """GET a Roblox URL and return ``(status, json)``.

    Requests to games.roblox.com are paced, and HTTP 429 is retried with
    exponential backoff (honouring ``Retry-After`` when Roblox sends it).
    Returns the last status and ``None`` for the body on any failure —
    callers already treat "no data" as a soft miss, so nothing here raises.
    """
    pacer = _get_pacer() if pace else None
    backoff = 1.0
    status: int = 0
    for attempt in range(retries + 1):
        if pacer is not None:
            await pacer.acquire()
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                status = resp.status
                if status == 200:
                    try:
                        return status, await resp.json(content_type=None)
                    except Exception as exc:
                        logger.debug("Bad JSON from %s: %s", url, exc)
                        return status, None
                if status == 429 and attempt < retries:
                    wait = max(_retry_after_seconds(resp.headers), backoff)
                    wait += random.uniform(0, 0.3)
                    logger.warning(
                        "Rate limited (HTTP 429) by Roblox — retrying in %.1fs "
                        "(attempt %d/%d): %s",
                        wait, attempt + 1, retries, url.split("?")[0],
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 16.0)
                    continue
                return status, None
        except asyncio.TimeoutError:
            logger.debug("Roblox request timed out: %s", url)
            return 0, None
        except aiohttp.ClientError as exc:
            logger.debug("Roblox request error (%s): %s", type(exc).__name__, exc)
            if attempt >= retries:
                return status, None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 16.0)
        except Exception as exc:
            logger.debug("Unexpected Roblox request error: %r", exc)
            return status, None
    return status, None


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

    # Chunk at the endpoint's own limit — 50, not 100. See _DETAILS_BATCH_SIZE.
    batches = [
        universe_ids[i:i + _DETAILS_BATCH_SIZE]
        for i in range(0, len(universe_ids), _DETAILS_BATCH_SIZE)
    ]
    results: list[Optional[dict]] = []

    async def _fetch_batch(batch_ids: list[int]) -> dict[int, dict]:
        url = f"{_GAMES_API}/games?universeIds={','.join(str(uid) for uid in batch_ids)}"
        async with _get_semaphore():
            status, data = await request_json(session, url, timeout=15)
            if data is None:
                # WARNING, not DEBUG: a rejected chunk means every game in it
                # silently disappears from the scan, which is exactly the kind
                # of failure that used to go unnoticed for weeks.
                logger.warning(
                    "Batch details fetch returned HTTP %s for %d universe ids — "
                    "those games are skipped this cycle",
                    status,
                    len(batch_ids),
                )
                return {}
            return _normalize_batch(data.get("data", []), batch_ids)

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
        status, data = await request_json(session, url, timeout=10)
    if data is None:
        logger.debug("Rating fetch returned HTTP %s for universe %s", status, universe_id)
        return None
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
    return None


async def fetch_game_ratings_batch(
    session: aiohttp.ClientSession,
    universe_ids: list[int],
) -> dict[int, dict]:
    """
    Fetch ratings for many games in as few requests as possible.

    GET /v1/games/votes?universeIds={id1},{id2},...  (chunked to 100)

    Returns {universe_id: rating_dict} for ratings that were found. One
    request per 100 games instead of one per game matters a lot when the
    host's IP is already being rate-limited.
    """
    if not universe_ids:
        return {}

    result_map: dict[int, dict] = {}
    for chunk in _chunks(list(dict.fromkeys(universe_ids)), _BATCH_SIZE):
        url = f"{_GAMES_API}/games/votes?universeIds={','.join(str(uid) for uid in chunk)}"
        async with _get_semaphore():
            status, data = await request_json(session, url, timeout=15)
        if data is None:
            logger.debug("Batch ratings fetch returned HTTP %s", status)
            continue
        for item in data.get("data", []):
            uid = item.get("universeId") or item.get("id")
            if not uid:
                continue
            up = item.get("upVotes", 0)
            down = item.get("downVotes", 0)
            total = up + down
            ratio = (up / total * 100.0) if total > 0 else 0.0
            result_map[int(uid)] = {
                "up_votes": up,
                "down_votes": down,
                "total_votes": total,
                "rating_percent": round(ratio, 1),
            }

    return result_map


async def fetch_thumbnails_batch(
    session: aiohttp.ClientSession,
    universe_ids: list[int],
    size: str = "512x512",
    format: str = "Png",
) -> dict[int, str]:
    """
    Fetch thumbnail URLs for many games (up to 100 per request).

    Uses thumbnails.roblox.com rather than games.roblox.com: the
    per-universe ``/v1/games/{id}/icon`` route now returns 404 for every
    request, and the thumbnails host is not throttled as aggressively.
    """
    if not universe_ids:
        return {}

    out: dict[int, str] = {}
    for chunk in _chunks(list(dict.fromkeys(universe_ids)), _BATCH_SIZE):
        url = (
            f"{_THUMBNAILS_API}/games/icons"
            f"?universeIds={','.join(str(uid) for uid in chunk)}"
            f"&size={size}&format={format}&isCircular=false"
        )
        async with _get_semaphore():
            # Not paced: thumbnails.roblox.com is a different host and has
            # never returned 429 on this deployment.
            status, data = await request_json(session, url, timeout=15, pace=False)
        if data is None:
            logger.debug("Thumbnail batch returned HTTP %s", status)
            continue
        for item in data.get("data", []):
            uid = item.get("targetId") or item.get("universeId")
            image = item.get("imageUrl")
            if uid and image:
                out[int(uid)] = image
    return out


async def fetch_game_thumbnail(
    session: aiohttp.ClientSession,
    universe_id: int,
    size: str = "512x512",
    format: str = "Png",
) -> Optional[str]:
    """Fetch one game's thumbnail URL (thin wrapper over the batch call)."""
    return (
        await fetch_thumbnails_batch(session, [universe_id], size=size, format=format)
    ).get(universe_id)


def fetch_game_description(game: dict) -> Optional[str]:
    """
    Return the description already carried by a normalized game dict.

    This used to hit ``/v1/games/multigame-details``, which now returns
    404 for every request — so descriptions were silently always None,
    which in turn made the Discord-invite-from-description lookup useless.
    The batch ``/v1/games`` payload already includes the description, so
    there is no reason to spend a request on it.
    """
    desc = (game.get("description") or "").strip()
    return desc or None


def fetch_creator_details(
    game: dict,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Return (creator_id, creator_type, creator_name) from a normalized game.

    Same story as the description: multigame-details is dead, and
    ``/v1/games`` already returns creator id / type / name.
    """
    creator_id = game.get("creator_id")
    if not creator_id:
        return None, None, None
    return (
        int(creator_id),
        game.get("creator_type", "User"),
        game.get("creator"),
    )


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
    Ensure a game dict carries its description text.

    No HTTP: the description is part of the ``/v1/games`` batch payload
    that produced this dict (see ``_normalize_batch``). The session arg is
    kept so existing call sites do not need to change.
    """
    desc = fetch_game_description(game)
    if desc:
        game["description"] = desc

    return game


async def enrich_games_with_ratings(
    session: aiohttp.ClientSession,
    games: list[dict],
) -> list[dict]:
    """
    Attach rating data to many games using a single batched request.

    Replaces N per-game ``/v1/games/votes`` calls with one call per 100
    games. Games with no rating data get ``rating_percent = 0.0`` so
    downstream filters see a consistent shape.
    """
    ids = [g["id"] for g in games if g.get("id")]
    ratings = await fetch_game_ratings_batch(session, ids)
    for game in games:
        rating = ratings.get(game.get("id"))
        if rating:
            game["rating_percent"] = rating["rating_percent"]
            game["up_votes"] = rating["up_votes"]
            game["down_votes"] = rating["down_votes"]
            game["total_votes"] = rating["total_votes"]
        else:
            game["rating_percent"] = 0.0
    return games


async def enrich_games_with_thumbnails(
    session: aiohttp.ClientSession,
    games: list[dict],
) -> list[dict]:
    """
    Attach thumbnail URLs to many games using a single batched request.

    Replaces N per-game icon lookups with one call per 100 games.
    """
    ids = [g["id"] for g in games if g.get("id")]
    thumbs = await fetch_thumbnails_batch(session, ids)
    for game in games:
        url = thumbs.get(game.get("id"))
        if url:
            game["thumbnail_url"] = url
    return games


def _chunks(items: list, size: int):
    """Yield successive size-sized chunks of items."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _normalize_batch(
    data: list[dict],
    requested_ids: list[int],
) -> dict[int, dict]:
    """
    Normalize Roblox API batch response to {universe_id: game_dict}.

    Only includes games that were present in the response. Description and
    creator id/type are carried through from the same payload so callers
    never need the (now dead) multigame-details endpoint.
    """
    result = {}
    for item in data:
        uid = item.get("universeId") or item.get("id")
        if not uid:
            continue
        uid = int(uid)
        creator = item.get("creator") or {}
        result[uid] = {
            "id": uid,
            "place_id": item.get("rootPlaceId", uid),
            "name": item.get("name", "Unknown"),
            "playing": item.get("playing", 0),
            "visits": item.get("visits", 0),
            "favorites": item.get("favoritedCount", 0),
            "creator": creator.get("name", "Unknown"),
            "creator_id": creator.get("id"),
            "creator_type": creator.get("type", "User"),
            "description": item.get("description", ""),
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