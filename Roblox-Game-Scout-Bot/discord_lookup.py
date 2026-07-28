"""
Discord invite lookup for Roblox games.

Finds Discord invite links from multiple Roblox sources:
  1. Roblox social links API (official Discord links on game pages)
  2. Game description text (parse for discord.gg/ or discord.com/invite/)
  3. Creator's Roblox group links
  4. Other fallback sources
"""

import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("discord_lookup")

# Discord invite URL patterns
_DISCORD_PATTERNS = [
    re.compile(r'(https?://)?(www\.)?discord\.(gg|com/invite)/[a-zA-Z0-9_-]+'),
    re.compile(r'(https?://)?(www\.)?discordapp\.com/invite/[a-zA-Z0-9_-]+'),
]

# Roblox API endpoints
_GAMES_API = "https://games.roblox.com/v1"
_GROUPS_API = "https://groups.roblox.com/v1"


async def find_discord_invite(
    session: aiohttp.ClientSession,
    universe_id: int,
    game_description: str = "",
    creator_id: Optional[int] = None,
    creator_type: Optional[str] = None,
) -> Optional[str]:
    """
    Find a Discord invite link for a Roblox game.

    Tries multiple sources in order:
      1. Roblox social links API
      2. Game description text
      3. Creator's group links (if Group creator)

    Returns the first valid Discord invite found, or None.
    """
    # Source 1: Roblox social links API
    invite = await _fetch_social_links(session, universe_id)
    if invite:
        logger.debug("Discord found via social links API for universe %s", universe_id)
        return invite

    # Source 2: Game description text
    if game_description:
        invite = _parse_description(game_description)
        if invite:
            logger.debug("Discord found in game description for universe %s", universe_id)
            return invite

    # Source 3: Creator's group links (if Group creator)
    if creator_id and creator_type == "Group":
        invite = await _fetch_group_links(session, creator_id)
        if invite:
            logger.debug("Discord found via group links for universe %s", universe_id)
            return invite

    return None


async def _fetch_social_links(
    session: aiohttp.ClientSession,
    universe_id: int,
) -> Optional[str]:
    """
    Fetch social links for a Roblox game from the official API.

    GET /v1/games/{universeId}/social-links/list
    Returns a list of social link objects with type and url.
    """
    url = f"{_GAMES_API}/games/{universe_id}/social-links/list"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for link in data.get("data", []):
                    link_type = link.get("type", "").lower()
                    link_url = link.get("url", "")
                    if link_type == "discord" and link_url:
                        return link_url
                    # Also check the URL directly for Discord patterns
                    if _is_discord_url(link_url):
                        return link_url
    except asyncio.TimeoutError:
        logger.debug("Social links API timeout for universe %s", universe_id)
    except Exception as exc:
        logger.debug("Social links API error for universe %s: %s", universe_id, exc)
    return None


async def _fetch_group_links(
    session: aiohttp.ClientSession,
    group_id: int,
) -> Optional[str]:
    """
    Fetch social links for a Roblox group.

    GET /v1/groups/{groupId}/social-links
    """
    url = f"{_GROUPS_API}/groups/{group_id}/social-links"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for link in data.get("data", []):
                    link_type = link.get("type", "").lower()
                    link_url = link.get("url", "")
                    if link_type == "discord" and link_url:
                        return link_url
                    if _is_discord_url(link_url):
                        return link_url
    except asyncio.TimeoutError:
        logger.debug("Group links API timeout for group %s", group_id)
    except Exception as exc:
        logger.debug("Group links API error for group %s: %s", group_id, exc)
    return None


def _parse_description(description: str) -> Optional[str]:
    """
    Parse a game description for Discord invite links.

    Looks for common Discord URL patterns in the text.
    """
    if not description:
        return None

    for pattern in _DISCORD_PATTERNS:
        match = pattern.search(description)
        if match:
            url = match.group(0)
            # Ensure it has a scheme
            if not url.startswith("http"):
                url = "https://" + url
            return url

    return None


def _is_discord_url(url: str) -> bool:
    """Check if a URL is a Discord invite link."""
    if not url:
        return False
    for pattern in _DISCORD_PATTERNS:
        if pattern.search(url):
            return True
    return False