"""
Scheduler for automated game scanning and alerting.

Runs a scan loop every SCAN_INTERVAL minutes:
  1. Collect games from all sources
  2. Enrich with ratings, thumbnails, Discord invites
  3. Run AI analysis
  4. Apply 6 hard filters — if ANY fails, log & skip
  5. Post alert embed to Discord channel
  6. Run watched-game tracker
"""

import asyncio
import logging
import time

import aiohttp
from discord.ext import tasks

from scanner import scan_games_async
from tracker import run_tracker
from database import (
    is_alerted,
    mark_alerted,
    save_alert_log,
)
from config import ALERT_CHANNEL_ID, SCAN_INTERVAL
from embeds import create_alert_embed
from buttons import AlertButtons
from filters import passes_alert_filters
from gemini_analyzer import analyze_game
from roblox_api_async import (
    enrich_game_with_ratings,
    enrich_game_with_thumbnail,
    enrich_game_with_description,
    fetch_creator_details,
)
from discord_lookup import find_discord_invite
from priority import (
    HIGH as PRIORITY_HIGH,
    MEDIUM as PRIORITY_MEDIUM,
    LOW as PRIORITY_LOW,
)

logger = logging.getLogger("scheduler")


def _format_log(scan_count, matched, duplicates, alerts, seconds, filtered_out):
    return (
        f"Scanning Roblox...\n"
        f"{scan_count} games checked\n"
        f"{matched} matched filters\n"
        f"{filtered_out} filtered out\n"
        f"{duplicates} duplicates skipped\n"
        f"{alerts} alerts posted\n"
        f"Completed in {seconds:.1f} seconds"
    )


def _classify_scan_priority(game):
    """Bucket a discovery-scan match into high / medium / low."""
    scout = game.get("scout_score") or {}
    score = scout.get("total", 0) if isinstance(scout, dict) else 0
    growth = float(game.get("growth", 0) or 0)

    if growth >= 100 or score >= 85:
        return PRIORITY_HIGH
    if growth >= 25 or score >= 70:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


async def _enrich_game_data(
    session: aiohttp.ClientSession,
    game: dict,
) -> dict:
    """
    Enrich a game dict with all data needed for filtering and alerts.

    Adds: rating_percent, thumbnail_url, description, discord_invite, creator_id/type
    """
    # Rating
    game = await enrich_game_with_ratings(session, game)

    # Thumbnail
    game = await enrich_game_with_thumbnail(session, game)

    # Description (needed for Discord invite lookup)
    game = await enrich_game_with_description(session, game)

    # Creator details (needed for Discord group links)
    creator_id, creator_type, _ = await fetch_creator_details(session, game.get("id", 0))
    if creator_id:
        game["creator_id"] = creator_id
        game["creator_type"] = creator_type

    # Discord invite from all sources
    discord_invite = await find_discord_invite(
        session,
        universe_id=game.get("id", 0),
        game_description=game.get("description", ""),
        creator_id=creator_id,
        creator_type=creator_type,
    )
    if discord_invite:
        game["discord_invite"] = discord_invite

    return game


async def _post_alert(channel, game):
    """Send a single alert embed with buttons to *channel*."""
    priority = _classify_scan_priority(game)
    embed = create_alert_embed(game, priority=priority)
    view = AlertButtons.with_game(game)
    await channel.send(embed=embed, view=view)


async def _run_scan_once(bot):
    """Run a single scan pass and post new opportunities to #alerts."""
    logger.info("Scheduled scan starting...")
    start_time = time.time()
    scan_count = 0
    matched = 0
    filtered_out = 0
    duplicates = 0
    alerts = 0

    try:
        async with aiohttp.ClientSession() as session:
            results = await scan_games_async(session=session)
            scan_count = len(results)
            save_alert_log("scan_complete", f"Scan completed: {scan_count} games")

            alert_channel = None
            if ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID):
                alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

            # Process each game through the enrichment + filter + alert pipeline
            for game in results:
                game_name = game.get("name", f"Game {game.get('id', '?')}")

                # Step 1: Enrich with ratings, thumbnails, Discord invite
                try:
                    game = await _enrich_game_data(session, game)
                except Exception as exc:
                    logger.warning("Enrichment failed for %s: %s", game_name, exc)
                    # Continue with whatever data we have

                # Step 2: Run AI analysis (cached 24h, fallback if Gemini unavailable)
                try:
                    if not is_alerted(game["id"]):
                        ai_result = analyze_game(game, force_refresh=False)
                        game["ai_analysis"] = ai_result
                except Exception as exc:
                    logger.debug("AI analysis failed for %s: %s", game_name, exc)
                    game["ai_analysis"] = {}

                # Step 3: Apply the 6 hard filters — ALL must pass
                passed, failures = passes_alert_filters(game)
                if not passed:
                    filtered_out += 1
                    save_alert_log(
                        "filter_rejected",
                        f"{game_name} (id={game.get('id')}): {' | '.join(failures)}",
                    )
                    continue

                matched += 1

                # Step 4: Check if already alerted
                if is_alerted(game["id"]):
                    duplicates += 1
                    continue

                mark_alerted(game["id"], game["name"])

                # Step 5: Post alert
                if alert_channel is not None:
                    try:
                        await _post_alert(alert_channel, game)
                        alerts += 1
                        logger.info("Alert posted: %s (id=%s)", game_name, game["id"])
                    except Exception as exc:
                        save_alert_log("alert_post_error", f"{game_name}: {exc}")
                        logger.exception("Failed to post alert for %s", game_name)
                else:
                    logger.warning(
                        "Skipping post: ALERT_CHANNEL_ID=%s not resolved.",
                        ALERT_CHANNEL_ID,
                    )

                # Small pacing delay to avoid rate limits
                await asyncio.sleep(0.5)

        seconds = time.time() - start_time
        save_alert_log(
            "cycle_summary",
            _format_log(scan_count, matched, duplicates, alerts, seconds, filtered_out),
        )
        logger.info(
            _format_log(scan_count, matched, duplicates, alerts, seconds, filtered_out)
        )
    except Exception as exc:
        save_alert_log("scan_error", f"Scan failed: {exc}")
        logger.exception("Scheduled scan failed")

    # Always run the watched-game tracker after the discovery scan
    try:
        await run_tracker(bot)
    except Exception as exc:
        save_alert_log("tracker_error", f"Tracker failed: {exc}")
        logger.exception("Tracker failed within scheduled cycle")


def start_scheduler(bot):
    """Start the scheduled scan loop on the given bot instance."""
    if not scheduled_scan.is_running():
        scheduled_scan.start(bot)
        logger.info("Scheduled scan task started (every %s minutes).", SCAN_INTERVAL)


def stop_scheduler():
    """Cancel the scheduled scan task if it is running."""
    if scheduled_scan.is_running():
        scheduled_scan.cancel()
        logger.info("Scheduled scan task cancelled.")


@tasks.loop(minutes=SCAN_INTERVAL)
async def scheduled_scan(bot):
    """Background loop that runs every SCAN_INTERVAL minutes."""
    await _run_scan_once(bot)


@scheduled_scan.before_loop
async def _before_scheduled_scan():
    """Wait until the bot is fully connected before starting the loop."""
    await asyncio.sleep(5)


@scheduled_scan.error
async def _scheduled_scan_error(exc):
    """Log errors raised by the loop so they never silently disappear."""
    logger.exception("Scheduled scan loop error: %s", exc)