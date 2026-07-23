import requests

# Default demo universe IDs to scan.
# These are the actual Roblox Universe IDs used by the /game command.
DEFAULT_SCAN_IDS = [
    2788229376,   # known test game
    994732206,    # Blox Fruits (estimated universe ID)
    380375386,    # Adopt Me (estimated universe ID)
    665070417,    # Jailbreak (estimated universe ID)
    559378614,    # Tower of Hell (estimated universe ID)
]


def search_games():
    """
    Return a list of game stubs to scan.

    Roblox's public discovery endpoint (/v1/games/list) is currently 404,
    so we use a small curated list of Universe IDs for the demo pipeline.
    """
    return [{"id": uid} for uid in DEFAULT_SCAN_IDS]
