import time
import discord
from categories import get_category
from opportunity import analyze_game, get_opportunity_level


def create_game_embed(game):
    """Create a Discord embed card for a Roblox game opportunity."""
    category = get_category(game)
    growth = game.get("growth", 0)
    growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"
    place_id = game.get("place_id", game["id"])

    embed = discord.Embed(
        title=f"🎮 {game['name']}",
        description=f"{category}  |  Source: {game.get('source', 'Unknown')}",
        color=discord.Color.green(),
        url=f"https://www.roblox.com/games/{place_id}",
    )

    embed.add_field(
        name="Opportunity",
        value=get_opportunity_level(game),
        inline=False
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
        value=f"[Open on Roblox](https://www.roblox.com/games/{place_id})",
        inline=True
    )

    embed.add_field(
        name="Why Found",
        value="\n".join(analyze_game(game)),
        inline=False
    )

    embed.set_footer(text=f"Universe ID: {game['id']} | Place ID: {place_id}")

    return embed


def create_alert_embed(game):
    """Create an alert embed for a newly discovered opportunity."""
    growth = game.get("growth", 0)
    growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"
    place_id = game.get("place_id", game["id"])
    opportunity = get_opportunity_level(game)

    if "🔥 HIGH" in opportunity:
        color = discord.Color.gold()
    elif "WATCHLIST" in opportunity:
        color = discord.Color.orange()
    else:
        color = discord.Color.blue()

    embed = discord.Embed(
        title=f"🚨 New Opportunity: {game['name']}",
        description=(
            f"{opportunity}\n"
            f"Source: {game.get('source', 'Unknown')}"
        ),
        color=color,
        url=f"https://www.roblox.com/games/{place_id}",
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(
        name="👥 Current Players",
        value=f"{game['playing']:,}",
        inline=True
    )

    embed.add_field(
        name="📈 Growth",
        value=growth_text,
        inline=True
    )

    embed.add_field(
        name="👀 Visits",
        value=f"{game['visits']:,}",
        inline=True
    )

    embed.add_field(
        name="👤 Creator",
        value=game.get("creator", "Unknown"),
        inline=True
    )

    embed.add_field(
        name="⭐ Scout Score",
        value=f"{game.get('score', 0)}/100",
        inline=True
    )

    embed.add_field(
        name="🔥 Trend",
        value=f"{game.get('trend_score', 0)}/100",
        inline=True
    )

    reasons = analyze_game(game)
    if not reasons:
        reasons = ["📊 Opportunity detected by routine scan"]
    embed.add_field(
        name="Reason",
        value="\n".join(f"• {r}" for r in reasons),
        inline=False
    )

    embed.set_footer(text=f"Universe ID: {game['id']} | Detected {time.strftime('%H:%M:%S')}")

    return embed
