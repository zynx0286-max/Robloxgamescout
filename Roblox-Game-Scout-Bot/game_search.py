"""
Curated game seed list and discovery sources.

Maintains a configurable seed list of universe IDs across genres.
This is the fallback when the search API is unavailable, and the
recommendation-seed source for /scan.

IMPORTANT: every ID below was verified live against
`games.roblox.com/v1/games?universeIds=...` on 2026-09. The previous list
was mostly wrong — famous *place* IDs used as universe IDs, which resolve
to unrelated junk games (0 CCU), plus several delisted universes. Run
`python validate_seeds.py` after editing to re-verify, and resolve new
entries from place IDs via
`apis.roblox.com/universes/v1/places/{placeId}/universe` first.
"""

# Verified universe IDs across genres (name = expected game on Roblox).
CURATED_SEED_IDS = [
    994732206,    # Blox Fruits
    383310974,    # Adopt Me!
    245662005,    # Jailbreak
    140239261,    # MeepCity
    66654135,     # Murder Mystery 2
    65241,        # Natural Disaster Survival
    2440500124,   # DOORS
    2380077519,   # Slap Battles
    4777817887,   # Blade Ball
    3317771874,   # Pet Simulator 99
    5750914919,   # Fisch
    5203828273,   # Dress To Impress
    7436755782,   # Grow a Garden
    7326934954,   # 99 Nights in the Forest
]


def get_curated_seeds() -> list[dict]:
    """Return the curated seed list as game stubs."""
    return [{"id": uid, "source": "Curated"} for uid in CURATED_SEED_IDS]


def search_games():
    """
    Return a list of game stubs to scan from the curated seed list.

    This is the synchronous fallback used when the async discovery engine
    is not available. The async engine (roblox_search.py) provides much
    broader coverage via keyword search.
    """
    return get_curated_seeds()
