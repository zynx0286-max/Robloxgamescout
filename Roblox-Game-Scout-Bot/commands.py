import discord
from discord.ext import commands
from utils import search_roblox_games, format_game_embed, get_game_details
from database import save_game, get_saved_games, remove_game
from config import MAX_RESULTS


class Scout(commands.Cog):
    """Commands for scouting Roblox games."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="scout", help="Search for Roblox games by keyword.")
    async def scout(self, ctx, *, query: str):
        """!scout <keyword> — Search Roblox for games matching a keyword."""
        async with ctx.typing():
            games = await search_roblox_games(query, limit=MAX_RESULTS)

        if not games:
            await ctx.send(f"No games found for **{query}**.")
            return

        for game in games:
            embed = format_game_embed(game)
            await ctx.send(embed=embed)

    @commands.command(name="gameinfo", help="Get details about a Roblox game by its Universe ID.")
    async def gameinfo(self, ctx, universe_id: int):
        """!gameinfo <universeId> — Fetch detailed info for a specific game."""
        async with ctx.typing():
            game = await get_game_details(universe_id)

        if not game:
            await ctx.send(f"Could not find a game with Universe ID `{universe_id}`.")
            return

        embed = format_game_embed(game, detailed=True)
        await ctx.send(embed=embed)

    @commands.command(name="save", help="Save a Roblox game to your watchlist.")
    async def save(self, ctx, universe_id: int):
        """!save <universeId> — Add a game to the saved watchlist."""
        game = await get_game_details(universe_id)
        if not game:
            await ctx.send(f"Game `{universe_id}` not found.")
            return

        await save_game(str(ctx.author.id), game)
        await ctx.send(f"✅ **{game['name']}** saved to your watchlist.")

    @commands.command(name="watchlist", help="Show your saved Roblox games.")
    async def watchlist(self, ctx):
        """!watchlist — View all games you've saved."""
        games = await get_saved_games(str(ctx.author.id))

        if not games:
            await ctx.send("Your watchlist is empty. Use `!save <universeId>` to add games.")
            return

        embed = discord.Embed(
            title=f"{ctx.author.display_name}'s Watchlist",
            color=discord.Color.blurple(),
        )
        for g in games:
            embed.add_field(
                name=g["name"],
                value=f"ID: `{g['universe_id']}` | Players: {g['playing']:,}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command(name="remove", help="Remove a game from your watchlist.")
    async def remove(self, ctx, universe_id: int):
        """!remove <universeId> — Remove a game from your watchlist."""
        removed = await remove_game(str(ctx.author.id), universe_id)
        if removed:
            await ctx.send(f"Removed game `{universe_id}` from your watchlist.")
        else:
            await ctx.send(f"Game `{universe_id}` was not in your watchlist.")

    @commands.command(name="help_scout", help="Show all Scout Bot commands.")
    async def help_scout(self, ctx):
        embed = discord.Embed(
            title="Roblox Game Scout Bot — Commands",
            color=discord.Color.green(),
        )
        embed.add_field(name="!scout <keyword>", value="Search Roblox games by keyword.", inline=False)
        embed.add_field(name="!gameinfo <universeId>", value="Get detailed info about a game.", inline=False)
        embed.add_field(name="!save <universeId>", value="Save a game to your watchlist.", inline=False)
        embed.add_field(name="!watchlist", value="View your saved games.", inline=False)
        embed.add_field(name="!remove <universeId>", value="Remove a game from your watchlist.", inline=False)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Scout(bot))
