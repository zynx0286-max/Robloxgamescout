import requests

# Default Universe IDs to scan.
# Roblox's public discovery endpoint is currently 404, so we use a curated list.
# To add more games, use the /game command with a Universe ID and confirm it
# returns real data, then add the ID here.
DEFAULT_SCAN_IDS = [
    994732206,    # Blox Fruits (verified)
]


def search_games():
    """
    Return a list of game stubs to scan.

    Roblox's public discovery endpoint (/v1/games/list) is currently 404,
    so we use a small curated list of Universe IDs for the demo pipeline.
    """
    return [{"id": uid} for uid in DEFAULT_SCAN_IDS]
