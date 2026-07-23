import requests
from bs4 import BeautifulSoup


def get_rotrends_games():
    """
    Fetch trending Roblox games from RoTrends.
    Extracts game links, names, and Universe IDs from the page.
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

    # Find links that lead to Roblox games
    links = soup.find_all("a", href=True)

    for link in links:
        href = link["href"]

        if "/game/" in href:
            name = link.text.strip()
            if not name:
                continue

            # Extract the Universe ID from URLs like /game/123456/Game-Name
            parts = href.split("/")
            game_id = None
            for part in parts:
                if part.isdigit():
                    game_id = int(part)
                    break

            if game_id is None:
                continue

            games.append({
                "id": game_id,
                "name": name,
                "source": "RoTrends",
                "url": href
            })

    return games[:20]


def get_trending_games():
    """Backward-compatible alias for get_rotrends_games."""
    return get_rotrends_games()
