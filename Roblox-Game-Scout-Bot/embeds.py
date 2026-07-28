"""
Discord embed builders for the Game Scout alert system.

Every alert embed must include:
  Game Name, Universe ID, Roblox Game Link, RoTrends Link,
  Discord Invite Link, Creator/Group, Current CCU, Total Visits,
  Rating (%), Favorites, Genre, Creation Date, Last Updated,
  Game Thumbnail/Icon, RoTrends Growth Data (if available),
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
    rotrends_url = game.get("rotrends_url") or f"https://rotrends.com/game/{universe_id}"
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
    embed.add_field(
        name="📊 RoTrends Link",
        value=f"[View on RoTrends]({rotrends_url})",
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

    # --- Row 5: RoTrends Growth Data ---
    if growth != 0:
        embed.add_field(
            name="📊 RoTrends Growth",
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