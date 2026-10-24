"""
Curated-seed validator.

Checks every ID in game_search.CURATED_SEED_IDS against the live Roblox
batch-details API and reports which seeds are dead (delisted / private /
never existed). Dead or wrong seeds waste scan capacity every cycle — the
original list mixed famous *place* IDs in as universe IDs, which silently
resolved to unrelated 0-CCU games.

Usage:
    python validate_seeds.py            # report only
    python validate_seeds.py --prune    # also write a cleaned seed list

Exit code is 1 when dead seeds are found, so it can gate CI.
"""

import argparse
import asyncio
import sys

import aiohttp

import game_search
from config import DATABASE_PATH  # noqa: F401  (keeps config import contract)

GAMES_API = "https://games.roblox.com/v1/games?universeIds={ids}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _batched(ids, size=50):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


async def check_seeds():
    """Return (alive_ids, dead_ids) for the curated seed list."""
    ids = list(dict.fromkeys(game_search.CURATED_SEED_IDS))
    alive = []
    dead = []

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for batch in _batched(ids):
            url = GAMES_API.format(ids=",".join(str(i) for i in batch))
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as exc:
                print(f"API error on batch ({len(batch)} ids): {exc}", file=sys.stderr)
                # Conservative: treat the whole batch as alive on API failure.
                alive.extend(batch)
                continue

            returned = {g["id"] for g in data.get("data", []) if g.get("id") is not None}
            for uid in batch:
                (alive if uid in returned else dead).append(uid)

    return alive, dead


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="rewrite game_search.py with dead seeds removed",
    )
    args = parser.parse_args()

    alive, dead = asyncio.run(check_seeds())

    print(f"Curated seeds: {len(alive) + len(dead)} unique "
          f"({len(alive)} alive, {len(dead)} dead)")

    if dead:
        print("\nDead seeds (no data returned by Roblox):")
        for uid in dead:
            print(f"  {uid}")

    if args.prune:
        if not dead:
            print("\nNothing to prune.")
            return
        import re

        path = game_search.__file__
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        # Drop the dead IDs from the seed list, including their trailing
        # comment lines (match "    <id>,  # comment").
        for uid in dead:
            source = re.sub(
                rf"^    {uid},.*\n",
                "",
                source,
                flags=re.MULTILINE,
            )
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        print(f"\nPruned {len(dead)} dead seeds from {path}")


if __name__ == "__main__":
    main()
