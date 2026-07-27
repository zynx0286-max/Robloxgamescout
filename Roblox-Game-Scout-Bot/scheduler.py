import asyncio
import logging
import time

from discord.ext import tasks

from scanner import scan_games
from database import (
    is_alerted,
    mark_alerted,
    save_alert_log,
)
from config import ALERT_CHANNEL_ID, SCAN_INTERVAL
from embeds import create_alert_embed
from buttons import AlertButtons

logger = logging.getLogger("scheduler")


def _format_log(scan_count, matched, duplicates, alerts, seconds):
    return (
        f"Scanning Roblox...\n"
        f"{scan_count} games checked\n"
        f"{matched} matched filters\n"
        f"{duplicates} duplicates skipped\n"
        f"{alerts} alert posted\n"
        f"Completed in {seconds:.1f} seconds"
    )


async def _post_alert(channel, game):
    """Send a single alert embed with buttons to *channel*."""
    embed = create_alert_embed(game)
    view = AlertButtons(game)
    await channel.send(embed=embed, view=view)


async def _run_scan_once(bot):
    """Run a single scan pass and post any new opportunities to #alerts."""
    logger.info("Scheduled scan starting...")
    start_time = time.time()
    scan_count = 0
    matched = 0
    duplicates = 0
    alerts = 0

    try:
        results = scan_games()
        scan_count = len(results)
        matched = len(results)
        save_alert_log("scan_complete", f"Scan completed: {scan_count} games")

        alert_channel = None
        if ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID):
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

        for game in results:
            if is_alerted(game["id"]):
                duplicates += 1
                continue

            mark_alerted(game["id"], game["name"])

            if alert_channel is not None:
                try:
                    await _post_alert(alert_channel, game)
                    alerts += 1
                    logger.info("Alert posted: %s (id=%s)", game["name"], game["id"])
                except Exception as exc:
                    save_alert_log("alert_post_error", f"{game['name']}: {exc}")
                    logger.exception("Failed to post alert for %s", game["name"])
            else:
                logger.warning(
                    "Skipping post: ALERT_CHANNEL_ID=%s not resolved.",
                    ALERT_CHANNEL_ID,
                )

            # Small pacing delay to avoid rate limits when many alerts post at once
            await asyncio.sleep(0.5)

        seconds = time.time() - start_time
        save_alert_log(
            "cycle_summary",
            _format_log(scan_count, matched, duplicates, alerts, seconds),
        )
        logger.info(_format_log(scan_count, matched, duplicates, alerts, seconds))
    except Exception as exc:
        save_alert_log("scan_error", f"Scan failed: {exc}")
        logger.exception("Scheduled scan failed")


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
