"""
Discord invite lookup for Roblox games.

Roblox used to expose the Discord link configured on a game's page through
two open endpoints:

    GET games.roblox.com/v1/games/{universeId}/social-links/list
    GET groups.roblox.com/v1/groups/{groupId}/social-links

Both were **withdrawn behind authentication** — verified live, each now
returns HTTP 401 ``{"code":9002,"message":"Authentication token is missing"}``
with or without a browser User-Agent. They were therefore removed rather than
left in place: every call was a guaranteed miss that still burned two
requests per game against the host that throttles this bot hardest.

That leaves the game description, which is where small and mid-sized Roblox
developers almost always put their Discord invite anyway, and which arrives
free with the batch ``/v1/games`` payload — so the whole lookup is now a
local string match with no network traffic at all.

The practical consequence: a Discord invite can only be *detected* when it is
written in the description. Games that link their Discord only through the
Roblox social-links feature will look like they have none, which is why
``require_discord`` defaults to off. Turn it on from /settings when you
specifically want games that advertise a Discord in their description.
"""

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


async def find_discord_invite(
    session: aiohttp.ClientSession,
    universe_id: int,
    game_description: str = "",
    creator_id: Optional[int] = None,
    creator_type: Optional[str] = None,
) -> Optional[str]:
    """Find a Discord invite link for a Roblox game.

    Only the description is consulted now (see the module docstring for why
    the social-link APIs were dropped). ``session``, ``creator_id`` and
    ``creator_type`` are accepted and ignored so existing call sites keep
    working unchanged, and so a future authenticated source can be added
    here without another signature change.
    """
    invite = _parse_description(game_description)
    if invite:
        logger.debug("Discord found in description for universe %s", universe_id)
    return invite


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


def has_discord_invite(game: dict) -> bool:
    """True when the game dict already carries a detectable Discord invite.

    Used by filters so the check does not have to re-import the regexes.
    """
    return bool(_parse_description(str(game.get("description") or "")))
