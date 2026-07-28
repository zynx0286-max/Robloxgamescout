"""
Game Scout filtering system.

A game can ONLY trigger an alert if ALL conditions pass.
If ANY requirement fails, log the reason and do not alert.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("filters")

# Hard filter thresholds
CCU_MIN = 15
CCU_MAX = 2500
VISITS_MAX = 1_500_000
RATING_MIN_PERCENT = 75.0


def _check_ccu(game: dict) -> tuple[bool, str]:
    """Check CCU (concurrent players) is between 15 and 2,500."""
    playing = game.get("playing", 0)
    if playing < CCU_MIN:
        return False, f"CCU {playing} below minimum {CCU_MIN}"
    if playing > CCU_MAX:
        return False, f"CCU {playing} above maximum {CCU_MAX}"
    return True, ""


def _check_visits(game: dict) -> tuple[bool, str]:
    """Check total visits is below 1,500,000."""
    visits = game.get("visits", 0)
    if visits >= VISITS_MAX:
        return False, f"Visits {visits:,} at or above limit {VISITS_MAX:,}"
    return True, ""


def _check_rating(game: dict) -> tuple[bool, str]:
    """Check rating percentage is at least 75%."""
    rating_pct = game.get("rating_percent", 0)
    if rating_pct <= 0:
        return False, "Rating data not available"
    if rating_pct < RATING_MIN_PERCENT:
        return False, f"Rating {rating_pct:.1f}% below minimum {RATING_MIN_PERCENT}%"
    return True, ""


def _check_discord(game: dict) -> tuple[bool, str]:
    """Check the game has a valid Discord invite link found from Roblox sources."""
    discord_invite = game.get("discord_invite", "")
    if not discord_invite:
        return False, "No Discord invite found via social links, description, or group"
    # Basic validation — should look like a Discord invite
    if not re.match(r'^(https?://)?(www\.)?(discord\.(gg|com/invite))/', discord_invite.strip()):
        return False, f"Discord link '{discord_invite}' does not appear to be a valid invite URL"
    return True, ""


def _check_roblox_link(game: dict) -> tuple[bool, str]:
    """Check the game generates a valid Roblox game URL."""
    place_id = game.get("place_id") or game.get("id")
    if not place_id:
        return False, "No place_id or id available to build Roblox link"
    # Store the generated link on the game dict for later use
    game["roblox_url"] = f"https://www.roblox.com/games/{place_id}"
    return True, ""


def _check_rotrends_link(game: dict) -> tuple[bool, str]:
    """Check the game generates a valid RoTrends URL."""
    universe_id = game.get("id")
    if not universe_id:
        return False, "No universe id available to build RoTrends link"
    # Store the generated link on the game dict for later use
    game["rotrends_url"] = f"https://rotrends.com/game/{universe_id}"
    return True, ""


def passes_alert_filters(game: dict) -> tuple[bool, list[str]]:
    """
    Run ALL filter checks against a game.

    Returns (passed: bool, failure_reasons: list[str]).
    If passed is True, the game qualifies for an alert.
    If passed is False, failure_reasons contains every reason it failed.
    """
    failures: list[str] = []

    # 1. CCU check
    ok, reason = _check_ccu(game)
    if not ok:
        failures.append(reason)

    # 2. Visits check
    ok, reason = _check_visits(game)
    if not ok:
        failures.append(reason)

    # 3. Rating check
    ok, reason = _check_rating(game)
    if not ok:
        failures.append(reason)

    # 4. Discord invite check
    ok, reason = _check_discord(game)
    if not ok:
        failures.append(reason)

    # 5. Roblox game link check
    ok, reason = _check_roblox_link(game)
    if not ok:
        failures.append(reason)

    # 6. RoTrends link check
    ok, reason = _check_rotrends_link(game)
    if not ok:
        failures.append(reason)

    passed = len(failures) == 0
    if not passed:
        game_name = game.get("name", f"Game {game.get('id', '?')}")
        logger.info(
            "FILTER FAILED: %s — %s",
            game_name,
            " | ".join(failures),
        )

    return passed, failures