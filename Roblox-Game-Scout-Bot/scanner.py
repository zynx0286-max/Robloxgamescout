"""Future home of the Roblox game scouting logic."""

from roblox_api import search_games


async def scout_games(keyword: str, min_visits: int = 0, min_players: int = 0):
    """Find games matching a keyword and filter by basic stats."""
    games = await search_games(keyword)

    results = []
    for game in games:
        visits = game.get("visits", 0)
        players = game.get("playerCount", 0)

        if visits >= min_visits and players >= min_players:
            results.append({
                "name": game.get("name", "Unknown"),
                "universe_id": game.get("universeId"),
                "visits": visits,
                "players": players,
                "creator": game.get("creator", {}).get("name", "Unknown"),
            })

    return results
