import aiohttp
import discord
from config import ROBLOX_API_BASE, ROBLOX_THUMBNAILS_API, MIN_ACTIVE_PLAYERS


def remove_duplicates(games):
    """Remove duplicate games by name (or id fallback) while preserving order."""
    seen = set()
    unique = []

    for game in games:
        key = game.get("name") or str(game.get("id"))

        if key and key not in seen:
            seen.add(key)
            unique.append(game)

    return unique


async def search_roblox_games(query: str, limit: int = 5) -> list[dict]:
    """Search Roblox for games matching *query* and return up to *limit* results."""
    url = f"{ROBLOX_API_BASE}/games/list"
    params = {
        "model.keyword": query,
        "model.startRows": 0,
        "model.maxRows": limit,
        "model.sortToken": "",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

    games = data.get("games", [])

    # Apply minimum player filter
    if MIN_ACTIVE_PLAYERS > 0:
        games = [g for g in games if g.get("playerCount", 0) >= MIN_ACTIVE_PLAYERS]

    # Normalise field names to a common schema
    return [_normalise(g) for g in games]


async def get_game_details(universe_id: int) -> dict | None:
    """Fetch full details for a single game by its Universe ID."""
    url = f"https://games.roblox.com/v1/games"
    params = {"universeIds": universe_id}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    games = data.get("data", [])
    if not games:
        return None

    return _normalise(games[0])


async def get_thumbnail(universe_id: int) -> str | None:
    """Return the thumbnail URL for a game, or None on failure."""
    url = f"{ROBLOX_THUMBNAILS_API}/games/icons"
    params = {
        "universeIds": universe_id,
        "returnPolicy": "PlaceHolder",
        "size": "256x256",
        "format": "Png",
        "isCircular": False,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    items = data.get("data", [])
    if items:
        return items[0].get("imageUrl")
    return None


def _normalise(raw: dict) -> dict:
    """Map raw Roblox API fields to a consistent schema."""
    return {
        "universeId": raw.get("universeId") or raw.get("id"),
        "name": raw.get("name", "Unknown"),
        "description": (raw.get("description") or "")[:300],
        "creator": raw.get("creator", {}).get("name", "Unknown"),
        "playing": raw.get("playing") or raw.get("playerCount", 0),
        "visits": raw.get("visits", 0),
        "maxPlayers": raw.get("maxPlayers", 0),
        "created": raw.get("created", ""),
        "updated": raw.get("updated", ""),
        "genre": raw.get("genre", "All"),
    }


def format_game_embed(game: dict, detailed: bool = False) -> discord.Embed:
    """Build a Discord embed for a game dict."""
    embed = discord.Embed(
        title=game["name"],
        url=f"https://www.roblox.com/games/{game['universeId']}",
        color=discord.Color.red(),
    )
    embed.add_field(name="Creator", value=game["creator"], inline=True)
    embed.add_field(name="Playing Now", value=f"{game['playing']:,}", inline=True)
    embed.add_field(name="Total Visits", value=f"{game['visits']:,}", inline=True)

    if detailed:
        embed.add_field(name="Max Players", value=str(game["maxPlayers"]), inline=True)
        embed.add_field(name="Genre", value=game["genre"], inline=True)
        if game["description"]:
            embed.add_field(name="Description", value=game["description"], inline=False)
        embed.add_field(name="Created", value=game["created"][:10] if game["created"] else "—", inline=True)
        embed.add_field(name="Last Updated", value=game["updated"][:10] if game["updated"] else "—", inline=True)

    embed.set_footer(text=f"Universe ID: {game['universeId']}")
    return embed
