from game_search import search_games
from rotrends import get_rotrends_games
from utils import remove_duplicates


def collect_games():
    """Collect games from all configured sources."""
    games = []

    # Roblox discovery
    for game in search_games():
        game["source"] = "Roblox"
        games.append(game)

    # Trend sources
    for game in get_rotrends_games():
        game["source"] = game.get("source", "RoTrends")
        games.append(game)

    return remove_duplicates(games)
