"""
Scanner system for Roblox Game Scout.

Coordinates game discovery, detail fetching, scoring, filtering, and ranking.
Supports both legacy sync and new async+concurrent modes.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from data_collector import collect_games, collect_games_async
from roblox_api import get_game_info
from roblox_api_async import fetch_game_details_batch
from database import save_game_snapshot
from growth import get_growth
from trend import calculate_trend_score
from filters import passes_alert_filters, passes_filters
from developer_intelligence import calculate_developer_score
from velocity import compute_velocity, get_trending_score_from_velocity
from config import MAX_CONCURRENT_FETCHES

logger = logging.getLogger("scanner")

DEBUG_LOG = "scan_debug.log"


def _log(message):
    """Append a debug line to the scan log file."""
    with open(DEBUG_LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def calculate_score(game):
    """Legacy flat score (0-100). Still used by /scoretest."""
    score = 0

    # Player activity
    if game["playing"] >= 1000:
        score += 25
    elif game["playing"] >= 100:
        score += 10

    # Visits
    if game["visits"] >= 1000000:
        score += 25
    elif game["visits"] >= 100000:
        score += 10

    # Favorites
    if game["favorites"] >= 50000:
        score += 20
    elif game["favorites"] >= 10000:
        score += 10

    # Growth trend
    growth = game.get("growth", 0)

    if growth >= 100:
        score += 30
    elif growth >= 50:
        score += 15

    return score


def _build_filter_diagnostics(game: dict, failures: list[str]) -> dict:
    """Return per-game filter diagnostics without changing the existing filter logic."""
    return {
        "ccu_pass": not any(reason.startswith("CCU") for reason in failures),
        "visits_pass": not any(reason.startswith("Visits") for reason in failures),
        "rating_pass": not any(reason.startswith("Rating") for reason in failures),
        "discord_present": bool(game.get("discord_invite")),
        "market_links_present": bool(game.get("market_links")),
    }


def _new_release_points(game):
    """Return (points, label, reason) for the New Release component."""
    stamp = game.get("created") or game.get("first_seen")
    if not stamp:
        return 0, "🆕 New release", "Release date unknown"

    try:
        from datetime import datetime, timezone

        iso = str(stamp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0, "🆕 New release", "Release date unresolved"

    if age_days <= 30:
        return 15, "🆕 New release", f"🔥 Hot launch ({age_days}d old)"
    if age_days <= 90:
        return 10, "🆕 New release", f"📅 Recent release ({age_days}d)"
    if age_days <= 180:
        return 5, "🆕 New release", f"📅 Modestly aged ({age_days}d)"
    return 0, "🆕 New release", f"⏳ Mature game ({age_days}d)"


async def _developer_history_points(game):
    """Pull creator history from Roblox via developer_intelligence and score it."""
    pts, label, reason = await calculate_developer_score(game)
    return pts, label, reason


async def calculate_scout_score(game):
    """Scout Score 2.0 — composable breakdown summing to 0-100."""
    breakdown = []

    # 📈 Growth (0-30)
    g = game.get("growth", 0)
    if g >= 100:
        growth_pts = 30
        growth_text = "🚀 Triple-digit growth"
    elif g >= 50:
        growth_pts = 25
        growth_text = "🚀 Strong growth"
    elif g >= 25:
        growth_pts = 20
        growth_text = "📈 Solid growth"
    elif g >= 10:
        growth_pts = 10
        growth_text = "📈 Mild growth"
    else:
        growth_pts = 0
        growth_text = "📊 Slow growth"
    breakdown.append(("📈 Growth", growth_pts, growth_text))

    # 👥 Player momentum (0-25)
    p = game.get("playing", 0)
    if p >= 10000:
        m_pts, m_text = 25, "🏟 Stadium-scale activity"
    elif p >= 5000:
        m_pts, m_text = 20, "🎯 Heavy momentum"
    elif p >= 1000:
        m_pts, m_text = 15, "🎯 Solid activity"
    elif p >= 100:
        m_pts, m_text = 8, "👥 Modest activity"
    else:
        m_pts, m_text = 0, "⏳ Quiet room"
    breakdown.append(("👥 Player momentum", m_pts, f"{m_text} ({p:,} CCU)"))

    # 🆕 New release bonus (0-15)
    nr_pts, nr_label, nr_text = _new_release_points(game)
    breakdown.append((nr_label, nr_pts, nr_text))

    # ❤️ Like ratio (0-10)
    visits = game.get("visits", 0) or 0
    favs = game.get("favorites", 0) or 0
    if visits > 0 and favs > 0:
        ratio = favs / visits
        if ratio >= 0.05:
            like_pts, like_text = 10, "❤️ Very high favor rate"
        elif ratio >= 0.02:
            like_pts, like_text = 7, "❤️ High favor rate"
        elif ratio >= 0.005:
            like_pts, like_text = 4, "❤️ Decent favor rate"
        else:
            like_pts, like_text = 0, "❤️ Low favor rate"
    else:
        like_pts, like_text = 0, "❤️ Favorites not yet tracked"
    breakdown.append(("❤️ Like ratio", like_pts, like_text))

    # 🚀 Velocity / Trending (0-20 — NEW)
    velocity = game.get("velocity", {})
    if velocity and velocity.get("snapshot_count", 0) >= 3:
        trend_pts = get_trending_score_from_velocity(velocity)
        trend_text = f"🚀 {velocity.get('ccu_trend', 'stable').title()} momentum"
        breakdown.append(("🚀 Velocity", trend_pts, trend_text))
    else:
        breakdown.append(("🚀 Velocity", 0, "📊 Insufficient history"))

    # 👨‍💻 Developer history (0-20 reserve — currently 0 until we add metrics)
    dev_pts, dev_label, dev_text = await _developer_history_points(game)
    breakdown.append((dev_label, dev_pts, dev_text))

    total = sum(pts for _, pts, _ in breakdown)
    return {"total": min(total, 100), "breakdown": breakdown, "verdict": _score_verdict(total)}


def _score_verdict(total):
    """Verdict label based on total score."""
    if total >= 80:
        return "🐐 Elite opportunity"
    if total >= 60:
        return "🔥 Strong opportunity"
    if total >= 40:
        return "📈 Worth watching"
    if total >= 20:
        return "👀 Mild signal"
    return "⚪ Low signal"


async def rank_games(games):
    """Attach both legacy score and Scout Score 2.0, then sort by Scout total."""
    for game in games:
        game["score"] = calculate_score(game)
        game["scout_score"] = await calculate_scout_score(game)

    games.sort(
        key=lambda x: x["scout_score"]["total"],
        reverse=True,
    )

    return games


async def _fetch_game_details_async(
    session: aiohttp.ClientSession,
    game_stubs: list[dict],
) -> list[dict]:
    """
    Fetch game details for multiple game stubs using batch API calls.

    This is the async counterpart to the sequential get_game_info loop.
    """
    if not game_stubs:
        return []

    # Collect all universe IDs
    universe_ids = [g["id"] for g in game_stubs]

    # Batch fetch game info
    batch_results = await fetch_game_details_batch(session, universe_ids)

    # Merge results back with stubs
    enriched = []
    for stub, details in zip(game_stubs, batch_results):
        if details is None:
            # Try async fallback
            try:
                info = await get_game_info(stub["id"], session=session)
                if info is None:
                    continue
                details = info
            except Exception as exc:
                logger.warning(f"Async fallback failed for game {stub.get('id')}: {exc}")
                continue

        details["source"] = stub.get("source", "Unknown")
        enriched.append(details)

    return enriched


def scan_games(user_settings=None):
    """Scan games from all sources, filter, save snapshots, and rank."""
    _log("scan_games started")

    if user_settings is None:
        user_settings = {
            "minimum_visits": 0,
            "minimum_players": 0,
            "minimum_growth": 0,
        }

    _log(f"settings: {user_settings}")

    found_games = []

    # Collect game stubs from all sources (now returns 150-500+ games)
    games = collect_games()
    _log(f"collected {len(games)} game stubs")

    for game in games:
        _log(f"processing game id={game.get('id')}")
        info = get_game_info(game["id"])
        _log(f"get_game_info result: {info is not None}")

        if info is None:
            continue

        info["source"] = game.get("source", "Unknown")
        save_game_snapshot(info)
        _log(f"saved snapshot for {info['name']}")

        growth = get_growth(info["id"])
        info["growth"] = growth
        info["trend_score"] = calculate_trend_score(info, growth)
        _log(f"growth={growth}, trend_score={info['trend_score']}")

        # Compute velocity metrics from historical data
        try:
            velocity = compute_velocity(info["id"])
            info["velocity"] = velocity
        except Exception as exc:
            _log(f"velocity computation failed: {exc}")
            info["velocity"] = {}

        if passes_filters(info, user_settings):
            _log(f"passes filters, adding {info['name']}")
            found_games.append(info)
        else:
            _log(
                f"filtered out: visits={info['visits']}, "
                f"players={info['playing']}, growth={info['growth']}"
            )

    _log(f"found {len(found_games)} games before ranking")
    ranked = rank_games(found_games)
    _log(f"returning {len(ranked)} ranked games")
    return ranked


async def scan_games_async(
    session: aiohttp.ClientSession,
    user_settings: Optional[dict] = None,
    is_deep_scan: bool = False,
    seed_ids: Optional[list[int]] = None,
    return_diagnostics: bool = False,
):
    """
    Async version of scan_games with concurrent batch fetching.

    This is the new high-performance scan pipeline. It:
      1. Collects game stubs from all sources concurrently
      2. Fetches game details in batches (up to 100 IDs per request)
      3. Computes growth, trend, and velocity for each game
      4. Filters and ranks results
    """
    _log("scan_games_async started")

    if user_settings is None:
        user_settings = {
            "minimum_visits": 0,
            "minimum_players": 0,
            "minimum_growth": 0,
        }

    # Step 1: Collect game stubs from all sources (150-500+ games)
    game_stubs = await collect_games_async(
        session,
        seed_ids=seed_ids,
        is_deep_scan=is_deep_scan,
    )
    _log(f"collected {len(game_stubs)} game stubs")

    if not game_stubs:
        _log("no games collected, returning empty")
        return []

    # Step 2: Fetch game details in batches
    enriched = await _fetch_game_details_async(session, game_stubs)
    _log(f"enriched {len(enriched)} games")

    # Step 3: Process each game (growth, trend, velocity, filtering)
    found_games = []
    diagnostics = {
        "total_collected": len(enriched),
        "ccu_passes": 0,
        "visits_passes": 0,
        "rating_passes": 0,
        "discord_present": 0,
        "market_links_present": 0,
        "final_matches": 0,
    }
    for info in enriched:
        try:
            # Save snapshot to history
            save_game_snapshot(info)

            # Compute growth from last two snapshots
            growth = get_growth(info["id"])
            info["growth"] = growth
            info["trend_score"] = calculate_trend_score(info, growth)

            # Compute velocity from historical data
            try:
                velocity = compute_velocity(info["id"])
                info["velocity"] = velocity
            except Exception:
                info["velocity"] = {}

            passed, failures = passes_alert_filters(info)
            filter_diagnostics = _build_filter_diagnostics(info, failures)
            if filter_diagnostics["ccu_pass"]:
                diagnostics["ccu_passes"] += 1
            if filter_diagnostics["visits_pass"]:
                diagnostics["visits_passes"] += 1
            if filter_diagnostics["rating_pass"]:
                diagnostics["rating_passes"] += 1
            if filter_diagnostics["discord_present"]:
                diagnostics["discord_present"] += 1
            if filter_diagnostics["market_links_present"]:
                diagnostics["market_links_present"] += 1

            # Apply user filters
            if passes_filters(info, user_settings):
                found_games.append(info)
            else:
                reason_text = " | ".join(failures) if failures else "unknown filter failure"
                _log(f"rejected game {info.get('id')} ({info.get('name')}): {reason_text}")
                logger.info("SCAN REJECTED: %s — %s", info.get("name", info.get("id")), reason_text)
        except Exception as exc:
            _log(f"error processing game {info.get('id')}: {exc}")
            continue

    # Step 4: Rank by Scout Score
    _log(f"found {len(found_games)} games before ranking")
    ranked = await rank_games(found_games)
    diagnostics["final_matches"] = len(ranked)
    _log(f"returning {len(ranked)} ranked games")
    if return_diagnostics:
        return ranked, diagnostics
    return ranked
