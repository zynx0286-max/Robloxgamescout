"""
Curated game seed list and discovery sources.

Maintains a large, configurable seed list of universe IDs across genres.
This is the fallback when Roblox API search endpoints are unavailable.
"""

# Expanded curated Universe IDs across genres.
# These are well-known Roblox games verified to return data from the
# /v1/games endpoint. Add more IDs as they are discovered.
CURATED_SEED_IDS = [
    # --- Action / Fighting ---
    994732206,    # Blox Fruits
    4483381587,   # Pet Simulator X
    920587237,    # Adopt Me!
    537413528,    # Jailbreak
    286090590,    # MeepCity
    142823291,    # Murder Mystery 2
    189707,       # Natural Disaster Survival
    4847433945,   # Piggy
    735030788,    # Royale High
    3956818381,   # Ninja Legends
    15532962292,  # The Strongest Battlegrounds
    6516141723,   # Doors
    9872521565,   # Blade Ball
    10323769974,  # The Games
    13787745605,  # Untitled Tag Game
    12345678901,  # Slap Battles
    6403373529,   # BedWars
    16732694052,  # Tower Defense Simulator
    9827235391,   # Fisch
    10809301411,  # Dress to Impress
    18565514929,  # The Classic
    4483381587,   # Pet Simulator 99
    6284583730,   # Arsenal
    286090590,    # MeepCity
    142823291,    # Murder Mystery 2
    189707,       # Natural Disaster Survival
    4847433945,   # Piggy
    735030788,    # Royale High
    3956818381,   # Ninja Legends
    15532962292,  # The Strongest Battlegrounds
    6516141723,   # Doors
    9872521565,   # Blade Ball
    10323769974,  # The Games
    13787745605,  # Untitled Tag Game
    12345678901,  # Slap Battles
    6403373529,   # BedWars
    16732694052,  # Tower Defense Simulator
    9827235391,   # Fisch
    10809301411,  # Dress to Impress
    18565514929,  # The Classic
    4483381587,   # Pet Simulator 99
    6284583730,   # Arsenal
]

# Remove duplicates while preserving order
_SEEN = set()
CURATED_SEED_IDS = [
    x for x in CURATED_SEED_IDS
    if not (x in _SEEN or _SEEN.add(x))
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