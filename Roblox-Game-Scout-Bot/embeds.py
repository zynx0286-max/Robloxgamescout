import time
import discord
from categories import get_category
from opportunity import analyze_game, get_opportunity_level


def _format_breakdown_lines(scout_score):
    """Render a Scout Score breakdown as Markdown lines."""
    total = scout_score.get("total", 0)
    verdict = scout_score.get("verdict", "")
    lines = [f"**⭐ Scout Score: {total}/100** — {verdict}"]

    for label, pts, reason in scout_score.get("breakdown", []):
        lines.append(f"{label}: **+{pts}** pts  ·  {reason}")

    return "\n".join(lines)


def create_game_embed(game):
    """Create a Discord embed card for a Roblox game opportunity."""
    category = get_category(game)
    growth = game.get("growth", 0)
    growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"
    place_id = game.get("place_id", game["id"])
    scout = game.get("scout_score") or {"total": game.get("score", 0), "breakdown": []}

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
        name="⭐ Scout Score Breakdown",
        value=_format_breakdown_lines(scout),
        inline=False
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
    scout = game.get("scout_score") or {"total": game.get("score", 0), "breakdown": []}

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
        value=_format_breakdown_lines(scout),
        inline=False
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


def create_tracker_embed(
    game_name,
    old_players,
    new_players,
    old_visits,
    new_visits,
    players_delta,
    visits_delta,
    tracked_since="",
):
    """Build a '🚀 Watched Game Explosion' card for the tracker channel."""
    players_pct = (
        f"+{players_delta:.1f}%" if players_delta >= 0 else f"{players_delta:.1f}%"
    )
    visits_pct = (
        f"+{visits_delta:.1f}%" if visits_delta >= 0 else f"{visits_delta:.1f}%"
    )

    if players_delta >= 50 or visits_delta >= 20:
        color = discord.Color.gold()
        severity = "🚀 MAJOR EXPLOSION"
    elif players_delta >= PLAYERS_THRESHOLD_TEXT or visits_delta >= VISITS_THRESHOLD_TEXT:
        color = discord.Color.orange()
        severity = "🚀 Watched Game Activity"
    else:
        color = discord.Color.blue()
        severity = "👀 Watched Game Change"

    embed = discord.Embed(
        title=severity,
        description=f"**{game_name}** is gaining serious momentum!",
        color=color,
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(
        name="👥 Players",
        value=f"{int(old_players):,} → {int(new_players):,}",
        inline=False,
    )

    embed.add_field(
        name="📈 Growth",
        value=players_pct,
        inline=True
    )

    embed.add_field(
        name="👀 Visits",
        value=f"{int(old_visits):,} → {int(new_visits):,}",
        inline=False,
    )

    embed.add_field(
        name="📊 Visits Δ",
        value=visits_pct,
        inline=True
    )

    embed.add_field(
        name="⏱ Tracked since",
        value=(tracked_since[:10] if tracked_since else "—"),
        inline=True,
    )

    embed.add_field(
        name="🔎 Reason",
        value="Large CCU increase" if players_delta >= 50 else "Significant movement",
        inline=False,
    )

    embed.set_footer(text="Game Scout tracker")

    return embed


# Module-level thresholds used by create_tracker_embed (mirror tracker.py values).
PLAYERS_THRESHOLD_TEXT = 20
VISITS_THRESHOLD_TEXT = 5
