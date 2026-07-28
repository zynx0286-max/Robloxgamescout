import asyncio
import aiohttp


async def get_game_info(place_id, retries=3, delay=0.5):
    """Fetch Roblox game info with retries (async version)."""
    url = f"https://games.roblox.com/v1/games?universeIds={place_id}"

    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status_code == 200:
                        data = await response.json()
                        if data.get("data"):
                            game = data["data"][0]
                            return {
                                "id": place_id,
                                "place_id": game.get("rootPlaceId", place_id),
                                "name": game["name"],
                                "playing": game["playing"],
                                "visits": game["visits"],
                                "favorites": game["favoritedCount"],
                                "creator": game["creator"]["name"]
                            }

                    if response.status_code == 429:
                        await asyncio.sleep(delay * (attempt + 1))
                        continue

        except Exception:
            pass

        if attempt < retries - 1:
            await asyncio.sleep(delay)

    return None


# Keep a sync wrapper for backward compatibility with non-async code
def get_game_info_sync(place_id, retries=3, delay=0.5):
    """Fetch Roblox game info with retries (sync wrapper for backward compatibility)."""
    import requests
    import time
    
    url = f"https://games.roblox.com/v1/games?universeIds={place_id}"

    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data.get("data"):
                    game = data["data"][0]
                    return {
                        "id": place_id,
                        "place_id": game.get("rootPlaceId", place_id),
                        "name": game["name"],
                        "playing": game["playing"],
                        "visits": game["visits"],
                        "favorites": game["favoritedCount"],
                        "creator": game["creator"]["name"]
                    }

            if response.status_code == 429:
                time.sleep(delay * (attempt + 1))
                continue

        except Exception:
            pass

        if attempt < retries - 1:
            time.sleep(delay)

    return None
