"""
Scanner system for Roblox Game Scout.

Coordinates game discovery, detail fetching, scoring, filtering, and ranking.
Supports both legacy sync and new async+concurrent modes.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

from data_collector import collect_games, collect_games_async
from roblox_api import get_game_info
from roblox_api_async import (
    enrich_games_with_ratings,
    enrich_games_with_thumbnails,
    fetch_game_details_batch,
)
from database import save_game_snapshot, save_game_snapshots_bulk
from growth import get_growth
from history import growth_summary, growth_summaries_bulk as history_growth_bulk
from trend import calculate_trend_score
from filters import passes_alert_filters, without_discord, format_rejection_summary
import chart_rank
from discord_lookup import find_discord_invite
from developer_intelligence import calculate_developer_score
from velocity import (
    compute_velocity,
    compute_velocities_bulk,
    get_trending_score_from_velocity,
)
from config import MAX_CONCURRENT_FETCHES

logger = logging.getLogger("scanner")

DEBUG_LOG = "scan_debug.log"
DEBUG_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation


def _log(message):
    """Append a debug line to the scan log file.

    Explicit UTF-8 with replacement, and never raises. Game names are full of
    emoji, and the default encoding on Windows (cp1252) cannot encode them —
    that UnicodeEncodeError used to propagate out of the scan loop and abort
    the entire cycle the moment a rejected game had an emoji in its name.
    A debug log must not be able to kill a scan.

    Also size-capped: the bot runs 24/7 on a small host, and an append-only
    log growing forever eventually fills the disk. At the cap the file is
    rotated to ``scan_debug.log.1`` (one generation kept).
    """
    try:
        try:
            if os.path.getsize(DEBUG_LOG) >= DEBUG_LOG_MAX_BYTES:
                os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
        except OSError:
            pass  # file missing or locked — appending still works
        with open(DEBUG_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


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


# filter_diagnostics key -> cumulative counter in the scan diagnostics block.
_DIAGNOSTIC_COUNTERS = (
    ("ccu_pass", "ccu_passes"),
    ("visits_pass", "visits_passes"),
    ("rating_pass", "rating_passes"),
    ("discord_present", "discord_present"),
    ("charts_present", "charts_present"),
    ("genre_pass", "genre_passes"),
    ("age_pass", "age_passes"),
    ("market_links_present", "market_links_present"),
)


def _build_filter_diagnostics(game: dict, failures: list[str]) -> dict:
    """Derive per-check pass/fail flags from the failure reasons.

    Prefixes rather than substring matches: "Genre 'x' does not match 'y'"
    must not be confused with any other reason that happens to contain the
    word "genre".
    """
    def _passed(prefix: str) -> bool:
        return not any(reason.startswith(prefix) for reason in failures)

    return {
        "ccu_pass": _passed("CCU"),
        "visits_pass": _passed("Visits"),
        "rating_pass": _passed("Rating"),
        "discord_present": bool(game.get("discord_invite")),
        "charts_present": bool(game.get("on_charts")),
        "genre_pass": _passed("Genre"),
        "age_pass": _passed("Age"),
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

    # 🚀 Velocity / Trending (0-20)
    # get_trending_score_from_velocity returns a 0-100 index — scale it into
    # this component's 0-20 budget, or the totals of every fast-growing game
    # overflow their component caps and the 100-point clamp silently
    # flattens honest scores.
    velocity = game.get("velocity", {})
    if velocity and velocity.get("snapshot_count", 0) >= 3:
        trend_pts = min(20, round(get_trending_score_from_velocity(velocity) * 20 / 100))
        trend_text = f"🚀 {velocity.get('ccu_trend', 'stable').title()} momentum"
        breakdown.append(("🚀 Velocity", trend_pts, trend_text))
    else:
        breakdown.append(("🚀 Velocity", 0, "📊 Insufficient history"))

    # 📊 Chart rank (0-10)
    # A game's position on a Roblox chart — and whether it is climbing — is
    # often the strongest signal available for a brand-new game, which has
    # neither the velocity history nor the favor rate the older components
    # feed on.
    breakdown.append(
        ("📊 Chart rank", chart_rank.chart_score(game), chart_rank.chart_score_text(game))
    )

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
        # Chart membership is decided once, in discovery (data_collector),
        # against the chart sorts fetched for that cycle. The batch normalizer
        # builds brand-new dicts, so the flag has to be carried across here or
        # require_charts sees None for every game and rejects all of them.
        if "on_charts" in stub:
            details["on_charts"] = stub["on_charts"]
        if stub.get("charts_sort"):
            details["charts_sort"] = stub["charts_sort"]
        # Chart-rank fields travel the same road as on_charts: the batch
        # normalizer builds fresh dicts, so anything discovery learned has
        # to be re-attached here.
        if "chart_appearances" in stub:
            details["chart_appearances"] = stub["chart_appearances"]
        if stub.get("chart_rank"):
            details["chart_rank"] = stub["chart_rank"]
        if stub.get("charts_sort_id"):
            details["charts_sort_id"] = stub["charts_sort_id"]
        enriched.append(details)

    return enriched


def scan_games(user_settings=None):
    """Legacy sync entry point — thin wrapper over scan_games_async.

    The historical implementation called asyncio.run(get_game_info(...))
    once per game inside a loop, spinning up and tearing down an event loop
    for every single game (and raising RuntimeError when a loop was already
    running). The async pipeline fetches in batches and is dramatically
    faster, so this wrapper just delegates to it.
    """
    _log("scan_games (legacy wrapper) started")

    async def _run():
        async with aiohttp.ClientSession() as session:
            return await scan_games_async(session, user_settings=user_settings)

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        # asyncio.run() cannot be nested inside a running event loop.
        logger.error(
            "scan_games() is synchronous and cannot run inside an event "
            "loop (%s). Use scan_games_async() instead.", exc
        )
        raise


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

    # Step 2b: Bulk ratings + thumbnails — one request per 100 games each.
    # This has to happen HERE, before the filters below, because the rating
    # filter rejects any game without rating data. Enriching after the scan
    # returned (as the scheduler used to) meant every candidate was judged
    # unrated and the scan came back empty no matter what.
    try:
        await enrich_games_with_ratings(session, enriched)
        await enrich_games_with_thumbnails(session, enriched)
    except Exception as exc:
        logger.warning("Batch enrichment failed: %s", exc)

    # Step 3: Process each game (snapshot, growth, trend, velocity) and run
    # the cheap filters.
    #
    # The Discord requirement is deliberately suspended for this pass: the
    # lookup costs two extra Roblox requests per game, and a game that fails
    # CCU/visits/rating/age would never alert regardless. Step 4 does the
    # lookup for survivors only, then applies the profile in full.
    candidates = []
    all_rejections: list[list[str]] = []
    diagnostics = {
        "total_collected": len(enriched),
        "ccu_passes": 0,
        "visits_passes": 0,
        "rating_passes": 0,
        "discord_present": 0,
        "charts_present": 0,
        "genre_passes": 0,
        "age_passes": 0,
        "market_links_present": 0,
        "final_matches": 0,
    }
    # Snapshot every collected game in ONE transaction before the per-game
    # loop: a scan writes ~150 rows, and one commit per row was ~150 fsyncs
    # per cycle. The per-game loop below then reads growth/velocity from
    # this fresh history exactly as before.
    try:
        save_game_snapshots_bulk(enriched)
    except Exception as exc:
        logger.warning("Bulk snapshot write failed (%s) — falling back per game", exc)
        for fallback_info in enriched:
            try:
                save_game_snapshot(fallback_info)
            except Exception:
                continue

    # Bulk local reads: growth-over-window and 72h velocity for every
    # candidate in one indexed query each — previously one connection +
    # query per game per metric (~300 connection setups/cycle at default
    # volume). Identical math, one read each; per-game helpers remain for
    # commands that look up a single game.
    all_ids = [info["id"] for info in enriched if info.get("id") is not None]
    try:
        bulk_growth = history_growth_bulk(all_ids)
    except Exception as exc:
        logger.warning("Bulk growth read failed (%s) — falling back per game", exc)
        bulk_growth = {}
    try:
        bulk_velocity = compute_velocities_bulk(all_ids)
    except Exception as exc:
        logger.warning("Bulk velocity read failed (%s) — falling back per game", exc)
        bulk_velocity = {}
    # Chart-rank signals: same bulk shape — two indexed queries for the
    # whole cycle instead of one connection + two queries per charted game.
    try:
        chart_rank.attach_rank_signals_bulk(enriched)
    except Exception as exc:
        logger.warning(
            "Bulk chart-rank read failed (%s) — falling back per game", exc
        )
        for info in enriched:
            try:
                chart_rank.attach_rank_signal(info)
            except Exception as per_game_exc:
                logger.debug(
                    "Chart rank lookup failed for %s: %s",
                    info.get("id"), per_game_exc,
                )

    for info in enriched:
        try:
            # Compute growth from last two snapshots
            growth = bulk_growth.get(info["id"], get_growth(info["id"]))
            info["growth"] = growth
            info["trend_score"] = calculate_trend_score(info, growth)

            # Velocity from the bulk read (falls back per game only if the
            # bulk pass above failed entirely).
            velocity = bulk_velocity.get(info["id"])
            if velocity is None:
                try:
                    velocity = compute_velocity(info["id"])
                except Exception:
                    velocity = {}
            info["velocity"] = velocity

            # Chart-rank signal: this cycle's position, movement vs the
            # previous cycle, and best-ever rank — from the bot's own
            # chart_positions store (written during discovery). A local
            # indexed read, so a charted game costs no network request.
            try:
                chart_rank.attach_rank_signal(info)
            except Exception as exc:
                logger.debug("Chart rank lookup failed for %s: %s", info.get("id"), exc)

            # Base thresholds plus the caller's /settings profile, minus the
            # Discord check (see above).
            passed, failures = passes_alert_filters(
                info, without_discord(user_settings)
            )
            filter_diagnostics = _build_filter_diagnostics(info, failures)
            for diag_key, counter_key in _DIAGNOSTIC_COUNTERS:
                if filter_diagnostics.get(diag_key):
                    diagnostics[counter_key] += 1

            if passed:
                candidates.append(info)
            else:
                all_rejections.append(failures)
                reason_text = " | ".join(failures) if failures else "unknown filter failure"
                _log(f"rejected game {info.get('id')} ({info.get('name')}): {reason_text}")
                logger.info("SCAN REJECTED: %s — %s", info.get("name", info.get("id")), reason_text)
        except Exception as exc:
            _log(f"error processing game {info.get('id')}: {exc}")
            continue

    # Step 4: Resolve Discord invites for the survivors, then apply the full
    # profile — the same one /profile prints — so require_discord means what
    # it says. (This path never populated discord_invite before, which is why
    # /scan returned nothing at all under the default profile.)
    try:
        survivor_growth = history_growth_bulk(
            [c["id"] for c in candidates if c.get("id") is not None]
        )
    except Exception:
        survivor_growth = {}
    found_games = []
    for info in candidates:
        try:
            invite = await find_discord_invite(
                session,
                universe_id=info.get("id", 0),
                game_description=info.get("description", ""),
                creator_id=info.get("creator_id"),
                creator_type=info.get("creator_type"),
            )
            if invite:
                info["discord_invite"] = invite
                diagnostics["discord_present"] += 1
        except Exception as exc:
            logger.debug("Discord lookup failed for %s: %s", info.get("name"), exc)

        passed, failures = passes_alert_filters(info, user_settings)
        if passed:
            # Attach the tracked-window growth figures the embeds render, from
            # the bulk survivor read above (one indexed query for all);
            # per-game fallback keeps behavior identical if it failed.
            summary = survivor_growth.get(info["id"])
            if not summary:
                try:
                    summary = growth_summary(info["id"])
                except Exception as exc:
                    logger.debug("History lookup failed for %s: %s", info.get("id"), exc)
                    summary = {}
            info.update(summary)
            found_games.append(info)
        else:
            all_rejections.append(failures)
            reason_text = " | ".join(failures) if failures else "unknown filter failure"
            _log(f"rejected game {info.get('id')} ({info.get('name')}): {reason_text}")
            logger.info(
                "SCAN REJECTED (post-enrichment): %s — %s",
                info.get("name", info.get("id")),
                reason_text,
            )

    # Step 5: Rank by Scout Score
    _log(f"found {len(found_games)} games before ranking")
    ranked = await rank_games(found_games)
    diagnostics["final_matches"] = len(ranked)
    _log(f"returning {len(ranked)} ranked games")

    # Silent-empty guard: a scan that matches nothing is almost always a
    # data bug (ratings never attached, chart flags dropped) rather than a
    # genuinely empty market — the dominant rejection category names the
    # culprit. Both historic empty-scan bugs would have been obvious here
    # on day one instead of surfacing as "why are alerts empty?" weeks later.
    rejection_note = ""
    if all_rejections:
        rejection_note = format_rejection_summary(
            all_rejections, collected=len(enriched)
        )
        if not ranked:
            logger.warning("SCAN EMPTY — %s", rejection_note)
            _log(f"scan matched 0 of {len(enriched)} games: {rejection_note}")
    diagnostics["rejection_note"] = rejection_note

    if return_diagnostics:
        return ranked, diagnostics
    return ranked
