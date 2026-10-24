"""
Game Scout filtering system.

Two tiers, evaluated together so a game passes only when both do:

1. **Base tier** — thresholds from the environment (`ALERT_*`). Applied to
   every alert the bot ever posts, including the scheduled ones that go to a
   shared channel nobody has a profile for.
2. **User tier** — the caller's `/settings` profile. Every stored value
   replaces the corresponding base value, so a user's filters are always
   enforced rather than merely displayed.

Historically the user tier only applied `minimum_visits`,
`minimum_players` and `minimum_growth`; the other seven options were saved,
printed by `/profile` and `/scan`, and then ignored. And three of the
`ALERT_*` env values were never read at all. Both of those are fixed here —
`evaluate()` is the single place any filter decision is made.

Every check returns a human-readable failure reason, and a rejected game logs
all of them, so "why didn't this game alert?" is always answerable.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from config import (
    ALERT_MIN_PLAYERS,
    ALERT_MAX_VISITS,
    ALERT_MIN_GROWTH,
    ALERT_REQUIRE_DISCORD,
)
from trending_sources import build_analytics_links

logger = logging.getLogger("filters")

# The CCU ceiling and the rating floor are not env-tunable: they are the core
# of the acquisition thesis (a big, well-liked game is not an acquisition
# target). Per-user settings still override both — a profile is an explicit
# statement of intent.
BASE_MIN_RATING = 75.0
BASE_MAX_PLAYERS = 2500

# `max_age` was never enforced, so the stored default of 365 was never a
# decision anyone made. Treating "no cap" as 0 keeps `/scan` finding the older,
# underperforming games this bot exists to surface; users who want a limit set
# one and it now takes effect.
DEFAULT_MAX_AGE = 0


def _num(value, default=0.0) -> float:
    """Coerce a value to float, tolerating None and junk."""
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def base_thresholds() -> dict:
    """Thresholds applied to every alert, before any per-user settings."""
    return {
        "min_players": ALERT_MIN_PLAYERS,
        "max_players": BASE_MAX_PLAYERS,
        "min_visits": 0,
        "max_visits": ALERT_MAX_VISITS,
        "min_growth": ALERT_MIN_GROWTH,
        "min_rating": BASE_MIN_RATING,
        "require_discord": ALERT_REQUIRE_DISCORD,
        "require_charts": False,
        "genre": "Any",
        "max_age": DEFAULT_MAX_AGE,
    }


def user_thresholds(settings: Optional[dict]) -> dict:
    """Translate a `/settings` row into threshold overrides.

    Only keys present in the settings dict are returned, so a partially-built
    dict (a test fixture, say) leaves the base tier intact elsewhere.
    """
    if not settings:
        return {}

    mapping = {
        "min_players": "minimum_players",
        "max_players": "maximum_players",
        "min_visits": "minimum_visits",
        "max_visits": "maximum_visits",
        "min_growth": "minimum_growth",
        "min_rating": "minimum_rating",
        "require_discord": "require_discord",
        "require_charts": "require_charts",
        "genre": "genre",
        "max_age": "max_age",
    }

    overrides = {}
    for threshold_key, setting_key in mapping.items():
        if setting_key not in settings:
            continue
        value = settings[setting_key]
        if value is None:
            continue
        overrides[threshold_key] = value

    return overrides


def thresholds_for(settings: Optional[dict] = None) -> dict:
    """Base thresholds with the user's profile layered on top."""
    thresholds = base_thresholds()
    thresholds.update(user_thresholds(settings))
    return thresholds


def without_discord(settings: Optional[dict]) -> dict:
    """A copy of a settings profile with the Discord requirement suspended.

    Finding a Discord invite costs two extra Roblox requests per game, so
    callers run the cheap filters first with this profile and only do the
    lookup for games that survive. The full profile is applied afterwards.
    """
    merged = dict(settings) if settings else {}
    merged["require_discord"] = False
    return merged


# --------------------------------------------------------------------------
# Individual checks — each returns (ok, reason)
# --------------------------------------------------------------------------

def _check_ccu(game: dict, t: dict) -> tuple[bool, str]:
    """CCU floor and ceiling. A ceiling of 0 means no cap."""
    playing = _num(game.get("playing"))
    if playing < _num(t.get("min_players")):
        return False, f"CCU {int(playing)} below minimum {int(_num(t.get('min_players')))}"
    max_players = _num(t.get("max_players"))
    if max_players and playing > max_players:
        return False, f"CCU {int(playing)} above maximum {int(max_players)}"
    return True, ""


def _check_visits(game: dict, t: dict) -> tuple[bool, str]:
    """Visit floor and ceiling. A ceiling of 0 means no cap."""
    visits = _num(game.get("visits"))
    if visits < _num(t.get("min_visits")):
        return False, (
            f"Visits {int(visits):,} below minimum {int(_num(t.get('min_visits'))):,}"
        )
    max_visits = _num(t.get("max_visits"))
    if max_visits and visits >= max_visits:
        return False, f"Visits {int(visits):,} at or above limit {int(max_visits):,}"
    return True, ""


def _check_growth(game: dict, t: dict) -> tuple[bool, str]:
    """Minimum growth percentage."""
    min_growth = _num(t.get("min_growth"))
    if min_growth <= 0:
        return True, ""
    growth = _num(game.get("growth"))
    if growth < min_growth:
        return False, f"Growth {growth:.1f}% below minimum {min_growth:.1f}%"
    return True, ""


def _check_rating(game: dict, t: dict) -> tuple[bool, str]:
    """Rating floor. A floor of 0 disables the check entirely.

    With a floor in place a game with no rating data is rejected rather than
    passed: an unrated game cannot be shown to satisfy the threshold, and the
    batch enrichment attaches ratings for every candidate in one request, so
    missing data means the game genuinely has too few votes to judge.
    """
    min_rating = _num(t.get("min_rating"))
    if min_rating <= 0:
        return True, ""
    rating_pct = _num(game.get("rating_percent"))
    if rating_pct <= 0:
        return False, "Rating data not available"
    if rating_pct < min_rating:
        return False, f"Rating {rating_pct:.1f}% below minimum {min_rating:.1f}%"
    return True, ""


def _check_discord(game: dict, t: dict) -> tuple[bool, str]:
    """Whether a Discord invite is required, and valid when required."""
    if not t.get("require_discord"):
        return True, ""
    discord_invite = game.get("discord_invite", "")
    if not discord_invite:
        return False, "No Discord invite found via social links, description, or group"
    if not re.match(
        r"^(https?://)?(www\.)?(discord\.(gg|com/invite))/",
        str(discord_invite).strip(),
    ):
        return False, f"Discord link '{discord_invite}' does not appear to be a valid invite URL"
    return True, ""


def _check_charts(game: dict, t: dict) -> tuple[bool, str]:
    """Whether the game must currently appear on a Roblox chart."""
    if not t.get("require_charts"):
        return True, ""
    if not game.get("on_charts"):
        return False, "Not on any Roblox chart this cycle"
    return True, ""


def _check_genre(game: dict, t: dict) -> tuple[bool, str]:
    """Genre match. "Any" or empty means no restriction."""
    wanted = str(t.get("genre") or "Any").strip()
    if not wanted or wanted.lower() == "any":
        return True, ""
    actual = str(game.get("genre") or "")
    if wanted.lower() not in actual.lower():
        return False, f"Genre '{actual or 'unknown'}' does not match '{wanted}'"
    return True, ""


def _check_age(game: dict, t: dict) -> tuple[bool, str]:
    """Maximum age in days. 0 means no cap; an unknown date passes."""
    max_age = _num(t.get("max_age"))
    if max_age <= 0:
        return True, ""
    stamp = game.get("created")
    if not stamp:
        return True, ""
    try:
        iso = str(stamp).replace("Z", "+00:00")
        created = datetime.fromisoformat(iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True, ""
    age_days = (datetime.now(timezone.utc) - created).days
    if age_days > max_age:
        return False, f"Age {age_days}d exceeds maximum {int(max_age)}d"
    return True, ""


def _check_roblox_link(game: dict, t: dict) -> tuple[bool, str]:
    """Build the Roblox game URL. Rejects only when there is no ID at all."""
    place_id = game.get("place_id") or game.get("id")
    if not place_id:
        return False, "No place_id or id available to build Roblox link"
    game["roblox_url"] = f"https://www.roblox.com/games/{place_id}"
    return True, ""


def _check_market_links(game: dict, t: dict) -> tuple[bool, str]:
    """Attach the Roblox-native analytics links used by embeds.

    A link can always be generated from a valid universe ID, so this never
    rejects a game — it only enriches the dict. (The former third-party links
    required no data either, which is why the old `require_market_links`
    setting could never filter anything.)
    """
    universe_id = game.get("id")
    if not universe_id:
        return False, "No universe id available to build analytics links"
    game["market_links"] = build_analytics_links(universe_id)
    return True, ""


_CHECKS = (
    _check_ccu,
    _check_visits,
    _check_growth,
    _check_rating,
    _check_discord,
    _check_charts,
    _check_genre,
    _check_age,
    _check_roblox_link,
    _check_market_links,
)


def evaluate(game: dict, thresholds: dict) -> tuple[bool, list[str]]:
    """Run every check against a resolved threshold set.

    Returns (passed, failure_reasons). All reasons are collected, not just the
    first, so a rejection log line explains everything that went wrong.
    """
    failures: list[str] = []
    for check in _CHECKS:
        ok, reason = check(game, thresholds)
        if not ok:
            failures.append(reason)

    passed = not failures
    if not passed:
        logger.info(
            "FILTER FAILED: %s — %s",
            game.get("name", f"Game {game.get('id', '?')}"),
            " | ".join(failures),
        )
    return passed, failures


def passes_alert_filters(
    game: dict, settings: Optional[dict] = None
) -> tuple[bool, list[str]]:
    """Evaluate a game against the base tier plus an optional user profile."""
    return evaluate(game, thresholds_for(settings))


def passes_user_filters(
    game: dict, settings: Optional[dict] = None
) -> tuple[bool, list[str]]:
    """Alias for passes_alert_filters — the user tier is never applied alone."""
    return passes_alert_filters(game, settings)


# --------------------------------------------------------------------------
# Rejection analytics — why a scan came back empty
# --------------------------------------------------------------------------

# Failure reasons start with a stable prefix (that is what scanner's
# diagnostics match on), so the category of a rejection is just its prefix
# mapped to a human label.
_REJECTION_CATEGORIES = (
    ("CCU", "CCU window"),
    ("Visits", "Visits window"),
    ("Rating", "Rating"),
    ("Growth", "Growth"),
    ("No Discord invite", "Discord invite"),
    ("Discord", "Discord invite"),
    ("Not on any Roblox chart", "Chart presence"),
    ("Genre", "Genre"),
    ("Age", "Age"),
)


def _rejection_category(reason: str) -> str:
    """Bucket one failure reason into a stable category label."""
    for prefix, label in _REJECTION_CATEGORIES:
        if reason.startswith(prefix):
            return label
    return "Other"


def summarize_rejections(rejections) -> list[tuple[str, int]]:
    """Tally filter failures across a scan, heaviest category first.

    rejections: iterable of failure-reason lists, exactly as
    filters.evaluate() returns them. Returns [(category, count), ...]
    sorted by count descending — the shape callers turn into a one-line
    warning like "top reasons: Rating (214), CCU window (30)".
    """
    tally: dict[str, int] = {}
    for failures in rejections or []:
        for reason in failures or []:
            label = _rejection_category(str(reason))
            tally[label] = tally.get(label, 0) + 1
    return sorted(tally.items(), key=lambda item: (-item[1], item[0]))


def format_rejection_summary(rejections, collected: int = None, top: int = 3) -> str:
    """One-line human summary of why games were rejected.

    Empty string when there is nothing to report. When *collected* is given
    the line leads with the match fraction, which is what makes an
    empty-scan bug legible: "0 of 257 games matched — top reasons: Rating
    (257)" says "the rating enrichment never ran", not "the market is dry".
    """
    tally = summarize_rejections(rejections)
    if not tally:
        return ""

    lead = ""
    if collected is not None:
        rejected_games = len(rejections or [])
        lead = f"{rejected_games} of {collected} games rejected — "

    reasons = ", ".join(f"{label} ({count})" for label, count in tally[:top])
    return f"{lead}top reasons: {reasons}"


def passes_filters(game: dict, user_settings: Optional[dict] = None) -> bool:
    """Boolean wrapper kept for the scanner's legacy call sites."""
    passed, failures = passes_alert_filters(game, user_settings)
    if not passed:
        logger.info(
            "FILTER FAILED: %s — %s",
            game.get("name", f"Game {game.get('id', '?')}"),
            " | ".join(failures),
        )
    return passed
