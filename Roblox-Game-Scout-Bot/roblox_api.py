import aiohttp

ROBLOX_API_BASE = "https://games.roblox.com/v1"


async def search_games(keyword: str, limit: int = 10):
    """Search Roblox for games matching a keyword."""
    url = f"{ROBLOX_API_BASE}/games/list"
    params = {
        "model.keyword": keyword,
        "model.startRows": 0,
        "model.maxRows": limit,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("games", [])


async def get_game_details(universe_id: int):
    """Fetch full details for a single game by Universe ID."""
    url = "https://games.roblox.com/v1/games"
    params = {"universeIds": universe_id}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            games = data.get("data", [])
            return games[0] if games else None
