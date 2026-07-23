import time
from data_collector import collect_games
from roblox_api import get_game_info
from database import save_game_snapshot
from growth import get_growth
from trend import calculate_trend_score
from filters import passes_filters


DEBUG_LOG = "scan_debug.log"


def _log(message):
    """Append a debug line to the scan log file."""
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


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
    """Scan games from all sources, filter, save snapshots, and rank."""
    _log("scan_games started")

    if user_settings is None:
        user_settings = {
            "minimum_visits": 0,
            "minimum_players": 0,
            "minimum_growth": 0,
        }

    _log(f"settings: {user_settings}")

    found_games = []

    games = collect_games()
    _log(f"collected {len(games)} games: {games}")

    for game in games:
        _log(f"processing game id={game.get('id')}")
        info = get_game_info(game["id"])
        _log(f"get_game_info result: {info is not None}")

        if info is None:
            continue

        info["source"] = game.get("source", "Unknown")
        save_game_snapshot(info)
        _log(f"saved snapshot for {info['name']}")

        growth = get_growth(info["id"])
        info["growth"] = growth
        info["trend_score"] = calculate_trend_score(info, growth)
        _log(f"growth={growth}, trend_score={info['trend_score']}")

        if passes_filters(info, user_settings):
            _log(f"passes filters, adding {info['name']}")
            found_games.append(info)
        else:
            _log(
                f"filtered out: visits={info['visits']}, "
                f"players={info['playing']}, growth={info['growth']}"
            )

    _log(f"found {len(found_games)} games before ranking")
    ranked = rank_games(found_games)
    _log(f"returning {len(ranked)} ranked games")
    return ranked
