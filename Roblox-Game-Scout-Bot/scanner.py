from game_search import search_games
from roblox_api import get_game_info
from database import save_game_snapshot
from growth import get_growth


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
    growth = get_growth(game["id"])
    game["growth"] = growth

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


def scan_games():
    found_games = []

    games = search_games()

    for game in games:
        info = get_game_info(
            game["id"]
        )

        if info:
            save_game_snapshot(info)
            found_games.append(info)

    return rank_games(found_games)
