import discord
from categories import get_category


def create_game_embed(game):
    """Create a Discord embed card for a Roblox game opportunity."""
    category = get_category(game)
    growth = game.get("growth", 0)
    growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"

    embed = discord.Embed(
        title=f"🎮 {game['name']}",
        description=f"{category}  |  Source: {game.get('source', 'Unknown')}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="👥 Players",
        value=f"{game['playing']:,}",
        inline=True
    )

    embed.add_field(
        name="👀 Visits",
        value=f"{game['visits']:,}",
        inline=True
    )

    embed.add_field(
        name="⭐ Score",
        value=f"{game.get('score', 0)}/100",
        inline=True
    )

    embed.add_field(
        name="📈 Growth",
        value=growth_text,
        inline=True
    )

    embed.add_field(
        name="🔥 Trend Score",
        value=f"{game.get('trend_score', 0)}/100",
        inline=True
    )

    embed.add_field(
        name="🌐 Game Link",
        value=f"[Open on Roblox](https://www.roblox.com/games/{game['id']})",
        inline=True
    )

    embed.set_footer(text=f"Universe ID: {game['id']}")

    return embed
