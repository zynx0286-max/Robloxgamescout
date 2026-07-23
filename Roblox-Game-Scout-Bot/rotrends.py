import requests
from bs4 import BeautifulSoup


def get_rotrends_games():
    """
    Fetch trending Roblox games from RoTrends.
    Currently tests the connection; full extraction comes next.
    """
    url = "https://rotrends.com"

    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    if response.status_code != 200:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    games = []

    # Placeholder: we will extract trending games here

    return games


def get_trending_games():
    """Backward-compatible alias for get_rotrends_games."""
    return get_rotrends_games()
