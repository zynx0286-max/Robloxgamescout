"""
Discord embed builders for the Game Scout alert system.

Every alert embed must include:
  Game Name, Universe ID, Roblox Game Link, Market Analytics Links
  (Roblox Charts, RoMonitor Stats, Creator Exchange),
  Discord Invite Link, Creator/Group, Current CCU, Total Visits,
  Rating (%), Favorites, Genre, Creation Date, Last Updated,
  Game Thumbnail/Icon, Growth Data (if available),
  AI Opportunity Score, AI Summary
"""

import time
from datetime import datetime

import discord

from priority import (
    HIGH as PRIORITY_HIGH,
    MEDIUM as PRIORITY_MEDIUM,
    LOW as PRIORITY_LOW,
)


def _format_date(iso_str: str) -> str:
    """Format an ISO date string to a readable format."""
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(iso_str)[:10] if iso_str else "Unknown"


def _safe_int(value, default=0) -> int:
    """Safely convert a value to int."""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def create_alert_embed(game: dict, priority: str = None) -> discord.Embed:
    """
    Create an alert embed for a game opportunity.

    Includes ALL required fields. If a field is missing from the game dict,
    it displays "N/A" or "Unknown".
    """
    # --- Extract all fields with safe defaults ---
    game_name = game.get("name", "Unknown Game")
    universe_id = game.get("id", "N/A")
    place_id = game.get("place_id") or universe_id
    roblox_url = game.get("roblox_url") or f"https://www.roblox.com/games/{place_id}"
    market_links = game.get("market_links") or {}
    if not market_links and isinstance(universe_id, int):
        from trending_sources import build_analytics_links
        market_links = build_analytics_links(universe_id)
    discord_invite = game.get("discord_invite", "Not found")
    creator = game.get("creator", "Unknown")
    current_ccu = f"{_safe_int(game.get('playing')):,}"
    total_visits = f"{_safe_int(game.get('visits')):,}"
    rating_pct = game.get("rating_percent", 0)
    rating_str = f"{_safe_float(rating_pct):.1f}%"
    favorites = f"{_safe_int(game.get('favorites')):,}"
    genre = game.get("genre", "Unknown") or "Unknown"
    created = _format_date(game.get("created"))
    updated = _format_date(game.get("updated"))
    thumbnail_url = game.get("thumbnail_url", "")
    growth = game.get("growth", 0)
    growth_str = f"+{_safe_float(growth):.1f}%" if _safe_float(growth) > 0 else f"{_safe_float(growth):.1f}%"

    # AI analysis fields
    ai_analysis = game.get("ai_analysis", {})
    ai_score = game.get("scout_score", {}).get("total", 0)
    ai_verdict = ai_analysis.get("verdict", "Not analyzed")
    ai_recommendation = ai_analysis.get("recommendation", "")
    ai_strengths = ai_analysis.get("strengths", [])
    ai_risks = ai_analysis.get("risks", [])

    # Build the AI summary
    ai_summary_parts = []
    if ai_verdict:
        ai_summary_parts.append(f"**Verdict:** {ai_verdict}")
    if ai_strengths:
        ai_summary_parts.append("**Strengths:** " + ", ".join(ai_strengths[:3]))
    if ai_risks:
        ai_summary_parts.append("**Risks:** " + ", ".join(ai_risks[:2]))
    if ai_recommendation:
        ai_summary_parts.append(f"**Recommendation:** {ai_recommendation}")
    ai_summary = "\n".join(ai_summary_parts) if ai_summary_parts else "AI analysis not yet available"

    # --- Priority-based styling ---
    if priority == PRIORITY_HIGH:
        color = discord.Color.gold()
        title_prefix = "🚨 BREAKOUT OPPORTUNITY"
    elif priority == PRIORITY_MEDIUM:
        color = discord.Color.orange()
        title_prefix = "📈 ACQUISITION TARGET"
    else:
        color = discord.Color.blue()
        title_prefix = "👀 GAME OPPORTUNITY"

    # --- Build the embed ---
    embed = discord.Embed(
        title=f"{title_prefix}: {game_name}",
        description=(
            f"**Universe ID:** {universe_id}\n"
            f"**Creator:** {creator}  |  **Genre:** {genre}"
        ),
        color=color,
        url=roblox_url,
        timestamp=discord.utils.utcnow(),
    )

    # Thumbnail (game icon)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    # --- Row 1: Core Stats ---
    embed.add_field(name="👥 Current CCU", value=current_ccu, inline=True)
    embed.add_field(name="👀 Total Visits", value=total_visits, inline=True)
    embed.add_field(name="⭐ Rating", value=rating_str, inline=True)

    # --- Row 2: More Stats ---
    embed.add_field(name="❤️ Favorites", value=favorites, inline=True)
    embed.add_field(name="📈 Growth", value=growth_str, inline=True)
    embed.add_field(name="🎯 Scout Score", value=f"{ai_score}/100", inline=True)

    # --- Row 3: Key Links ---
    embed.add_field(
        name="🔗 Roblox Link",
        value=f"[Open on Roblox]({roblox_url})",
        inline=False,
    )

    market_link_text = "\n".join(
        f"[{name}]({url})" for name, url in market_links.items()
    ) or "Not available"
    embed.add_field(
        name="📊 Market Links",
        value=market_link_text,
        inline=True,
    )
    embed.add_field(
        name="💬 Discord Invite",
        value=discord_invite if discord_invite and discord_invite != "Not found" else "Not found",
        inline=True,
    )

    # --- Row 4: Dates & Other ---
    embed.add_field(name="📅 Created", value=created, inline=True)
    embed.add_field(name="🔄 Last Updated", value=updated, inline=True)

    # --- Row 5: Growth Data ---
    if growth != 0:
        embed.add_field(
            name="📊 Growth Data",
            value=f"Player growth: {growth_str}\n"
                  f"Trend score: {game.get('trend_score', 'N/A')}/100",
            inline=False,
        )

    # --- Row 6: AI Analysis ---
    embed.add_field(
        name=f"🤖 AI Opportunity Score: {ai_score}/100",
        value=ai_summary if len(ai_summary) <= 1024 else ai_summary[:1021] + "...",
        inline=False,
    )

    embed.set_footer(
        text=f"Universe ID: {universe_id} | Detected {time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    )

    return embed


def create_game_embed(game: dict) -> discord.Embed:
    """
    Compatibility wrapper — delegates to create_alert_embed.

    Used by commands.py /scan and other callers that expect
    a function named create_game_embed.
    """
    return create_alert_embed(game)


def create_tracker_embed(
    game_name: str,
    old_players: int,
    new_players: int,
    old_visits: int,
    new_visits: int,
    players_delta: float,
    visits_delta: float,
    tracked_since: str,
    priority: str = None,
) -> discord.Embed:
    """
    Build an embed for a tracked-game CCU/visits alert.

    Used by commands.py /testtracker and the tracker module.
    """
    if priority == PRIORITY_HIGH:
        color = discord.Color.gold()
        title = f"🚨 BREAKOUT DETECTED: {game_name}"
    elif priority == PRIORITY_MEDIUM:
        color = discord.Color.orange()
        title = f"📈 GROWING OPPORTUNITY: {game_name}"
    else:
        color = discord.Color.green()
        title = f"📊 Tracker Alert: {game_name}"

    embed = discord.Embed(
        title=title,
        description=(
            f"**{game_name}** has seen a significant change in player activity."
        ),
        color=color,
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(
        name="👥 Players (Before)",
        value=f"{old_players:,}",
        inline=True,
    )
    embed.add_field(
        name="👥 Players (Now)",
        value=f"{new_players:,}",
        inline=True,
    )
    embed.add_field(
        name="📈 Player Change",
        value=f"{players_delta:+.1f}%",
        inline=True,
    )

    embed.add_field(
        name="👀 Visits (Before)",
        value=f"{old_visits:,}",
        inline=True,
    )
    embed.add_field(
        name="👀 Visits (Now)",
        value=f"{new_visits:,}",
        inline=True,
    )
    embed.add_field(
        name="📈 Visit Change",
        value=f"{visits_delta:+.1f}%",
        inline=True,
    )

    embed.add_field(
        name="📅 Tracked Since",
        value=tracked_since,
        inline=False,
    )

    embed.set_footer(
        text=f"Tracker Alert | {time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    )

    return embed
