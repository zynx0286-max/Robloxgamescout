"""Utility functions for the game scout bot."""

import hashlib
from typing import Any


def remove_duplicates(games: list[dict]) -> list[dict]:
    """
    Remove duplicate games from a list based on their 'id' field.

    Preserves order: the first occurrence of each ID is kept.
    """
    seen: set[int] = set()
    result: list[dict] = []
    for game in games:
        gid = game.get("id")
        if gid and gid not in seen:
            seen.add(gid)
            result.append(game)
    return result


def game_id_hash(game: dict) -> str:
    """Generate a stable hash for a game based on its ID."""
    gid = game.get("id", 0)
    return hashlib.md5(str(gid).encode()).hexdigest()[:8]