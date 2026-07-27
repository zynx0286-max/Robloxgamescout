import asyncio
import logging

from discord.ext import tasks

from scanner import scan_games
from database import (
    is_alerted,
    mark_alerted,
    save_alert_log,
)

logger = logging.getLogger("scheduler")

SCAN_INTERVAL_MINUTES = 15


def start_scheduler(bot):
    """Start the scheduled scan loop on the given bot instance."""
    if not scheduled_scan.is_running():
        scheduled_scan.start(bot)
        logger.info("Scheduled scan task started.")


def stop_scheduler():
    """Cancel the scheduled scan task if it is running."""
    if scheduled_scan.is_running():
        scheduled_scan.cancel()
        logger.info("Scheduled scan task cancelled.")


async def _run_scan_once(bot):
    """Run a single scan pass with basic logging and dedup."""
    logger.info("Scheduled scan starting...")
    save_alert_log("scan_start", "Scheduled scan started")

    try:
        results = scan_games()
        save_alert_log("scan_complete", f"Scan completed: {len(results)} games")

        new_alerts = []
        for game in results:
            if is_alerted(game["id"]):
                continue

            mark_alerted(game["id"], game["name"])
            new_alerts.append(game)
            logger.info(
                "New opportunity queued: %s (id=%s, score=%s)",
                game["name"],
                game["id"],
                game.get("score", 0),
            )

        save_alert_log(
            "new_alerts",
            f"{len(new_alerts)} new opportunities found",
        )
        logger.info(
            "Scheduled scan finished: %d results, %d new",
            len(results),
            len(new_alerts),
        )
        return new_alerts
    except Exception as exc:
        save_alert_log("scan_error", f"Scan failed: {exc}")
        logger.exception("Scheduled scan failed")
        return []


@tasks.loop(minutes=SCAN_INTERVAL_MINUTES)
async def scheduled_scan(bot):
    """Background loop that runs every SCAN_INTERVAL_MINUTES minutes."""
    await _run_scan_once(bot)


@scheduled_scan.before_loop
async def _before_scheduled_scan():
    """Wait until the bot is fully connected before starting the loop."""
    await asyncio.sleep(5)


@scheduled_scan.error
async def _scheduled_scan_error(exc):
    """Log errors raised by the loop so they never silently disappear."""
    logger.exception("Scheduled scan loop error: %s", exc)
