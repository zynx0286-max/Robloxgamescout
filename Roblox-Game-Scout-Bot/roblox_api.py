import requests


def get_game_info(place_id):
    url = f"https://games.roblox.com/v1/games?universeIds={place_id}"

    response = requests.get(url)

    if response.status_code != 200:
        return None

    data = response.json()

    if not data["data"]:
        return None

    game = data["data"][0]

    return {
        "id": place_id,
        "name": game["name"],
        "playing": game["playing"],
        "visits": game["visits"],
        "favorites": game["favoritedCount"],
        "creator": game["creator"]["name"]
    }
