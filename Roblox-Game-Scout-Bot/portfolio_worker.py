"""Portfolio live-data worker.

Polls the official Roblox Games API for every game on the portfolio and
writes a row to game_snapshots on each tick. The portfolio API then reads
those snapshots and computes growth — so Framer never talks to Roblox and
your visitors never trigger per-request Roblox calls.

Run as a long-lived process:

    python portfolio_worker.py            # loop, every PORTFOLIO_WORKER_INTERVAL
    python portfolio_worker.py --once     # single collection pass (cron-friendly)
"""

import argparse
import asyncio
import sys

import aiohttp

from config import PORTFOLIO_WORKER_INTERVAL
from portfolio_db import (
    create_portfolio_tables,
    list_portfolio_games,
    record_game_snapshot,
    update_portfolio_game,
)

GAMES_API = "https://games.roblox.com/v1/games?universeIds={ids}"
VOTES_API = "https://games.roblox.com/v1/games/votes?universeIds={ids}"
THUMBNAILS_API = (
    "https://thumbnails.roblox.com/v1/games/icons?universeIds={ids}"
    "&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _make_session():
    """ClientSession honoring HTTP(S)_PROXY env vars.

    Direct connections hang in proxied environments unless aiohttp opts in
    via trust_env. Safe when no proxy vars are set, too.
    """
    return aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=20),
        headers=HEADERS,
    )


def _batched(ids, size=50):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


async def _fetch_game_details(session, universe_ids):
    """Fetch full game metadata for a batch of universe IDs.

    Returns a {universe_id: dict} mapping. Missing IDs are skipped so one
    delisted game can't sink the whole batch.
    """
    details = {}
    for batch in _batched(universe_ids):
        url = GAMES_API.format(ids=",".join(str(i) for i in batch))
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    continue
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
        for game in data.get("data", []):
            uid = game.get("id")
            if uid is None:
                continue
            details[uid] = {
                "name": game.get("name") or "",
                "playing": game.get("playing"),
                "visits": game.get("visits"),
                "favorites": game.get("favoritedCount"),
                "creator": game.get("creator", {}).get("name", ""),
                "creator_type": game.get("creator", {}).get("type", ""),
                "created": game.get("created"),
                "updated": game.get("updated"),
                "genre": game.get("genre_l1") or game.get("genre") or "",
            }
    return details


async def _fetch_votes(session, universe_ids):
    """Fetch up/down vote counts. Returns a {universe_id: (up, down)} map."""
    votes = {}
    for batch in _batched(universe_ids):
        url = VOTES_API.format(ids=",".join(str(i) for i in batch))
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    continue
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
        for item in data.get("data", []):
            uid = item.get("id")
            if uid is not None:
                votes[uid] = (item.get("upVotes") or 0, item.get("downVotes") or 0)
    return votes


async def _fetch_thumbnails(session, universe_ids):
    """Fetch thumbnail URLs. Returns a {universe_id: url} mapping."""
    thumbnails = {}
    for batch in _batched(universe_ids):
        url = THUMBNAILS_API.format(ids=",".join(str(i) for i in batch))
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    continue
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
        for item in data.get("data", []):
            uid = item.get("targetId")
            url_value = item.get("imageUrl")
            if uid is not None and url_value:
                thumbnails[uid] = url_value
    return thumbnails


async def collect_portfolio(session=None):
    """Collect one snapshot for every visible portfolio game.

    Returns (collected, errors): counts of games snapshotted vs. failed.
    """
    games = list_portfolio_games(visible_only=True)
    if not games:
        return 0, 0

    own_session = session is None
    if own_session:
        session = _make_session()

    collected = 0
    errors = 0
    try:
        universe_ids = [g["game_id"] for g in games]
        details = await _fetch_game_details(session, universe_ids)
        votes = await _fetch_votes(session, universe_ids)
        thumbnails = await _fetch_thumbnails(session, universe_ids)

        for game in games:
            uid = game["game_id"]
            detail = details.get(uid)
            if detail is None:
                errors += 1
                continue
            up, down = votes.get(uid, (0, 0))
            record_game_snapshot(
                uid,
                detail["playing"],
                detail["visits"],
                favorites=detail["favorites"],
                likes=up,
                dislikes=down,
            )
            # Keep the thumbnail cache warm so the API never fetches it.
            if not game["thumbnail_url"] and uid in thumbnails:
                update_portfolio_game(uid, thumbnail_url=thumbnails[uid])
            collected += 1
    finally:
        if own_session:
            await session.close()

    return collected, errors


async def worker_loop(interval=None):
    interval = interval or PORTFOLIO_WORKER_INTERVAL
    print(f"Portfolio worker started — snapshot every {interval}s")
    while True:
        collected, errors = await collect_portfolio()
        print(
            f"[{asyncio.get_event_loop().time():.0f}] snapshotted "
            f"{collected} game(s), {errors} failed"
        )
        await asyncio.sleep(interval)


async def run_once():
    collected, errors = await collect_portfolio()
    print(f"collected {collected} snapshot(s), {errors} failed")
    return 0 if errors == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect a single snapshot and exit (cron-friendly)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=PORTFOLIO_WORKER_INTERVAL,
        help="seconds between snapshots (default: %(default)s)",
    )
    args = parser.parse_args()

    create_portfolio_tables()

    if args.once:
        sys.exit(asyncio.run(run_once()))
    try:
        asyncio.run(worker_loop(args.interval))
    except KeyboardInterrupt:
        print("\nworker stopped")


if __name__ == "__main__":
    main()
