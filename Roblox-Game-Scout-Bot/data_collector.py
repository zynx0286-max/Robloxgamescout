from game_search import search_games
from rotrends import get_trending_games


def collect_games():
    """Collect games from all configured sources."""
    games = []

    # Roblox discovery
    for game in search_games():
        game["source"] = "Roblox"
        games.append(game)

    # Trend sources
    for game in get_trending_games():
        game["source"] = game.get("source", "RoTrends")
        games.append(game)

    return games
