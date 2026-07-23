from game_search import search_games
from roblox_api import get_game_info
from database import save_game_snapshot
from growth import get_growth
from trend import calculate_trend_score
from filters import passes_filters


def calculate_score(game):
    score = 0

    # Player activity
    if game["playing"] >= 1000:
        score += 25
    elif game["playing"] >= 100:
        score += 10

    # Visits
    if game["visits"] >= 1000000:
        score += 25
    elif game["visits"] >= 100000:
        score += 10

    # Favorites
    if game["favorites"] >= 50000:
        score += 20
    elif game["favorites"] >= 10000:
        score += 10

    # Growth trend
    growth = game.get("growth", 0)

    if growth >= 100:
        score += 30
    elif growth >= 50:
        score += 15

    return score


def rank_games(games):
    for game in games:
        game["score"] = calculate_score(game)

    games.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return games


def scan_games(user_settings=None):
    """Scan games, filter by user settings, save snapshots, and rank results."""
    if user_settings is None:
        user_settings = {
            "minimum_visits": 0,
            "minimum_players": 0,
            "minimum_growth": 0,
        }

    found_games = []

    games = search_games()

    for game in games:
        info = get_game_info(game["id"])

        if info:
            save_game_snapshot(info)

            growth = get_growth(info["id"])
            info["growth"] = growth
            info["trend_score"] = calculate_trend_score(info, growth)

            if passes_filters(info, user_settings):
                found_games.append(info)

    return rank_games(found_games)
