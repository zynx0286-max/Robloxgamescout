import asyncio
import logging
import time

from roblox_api import get_game_info
from database import (
    get_all_watched_games,
    get_user_alert_level,
    update_watched_game_snapshot,
    save_alert_log,
)
from config import ALERT_CHANNEL_ID
from embeds import create_tracker_embed
from priority import (
    HIGH as PRIORITY_HIGH,
    MEDIUM as PRIORITY_MEDIUM,
    LOW as PRIORITY_LOW,
    should_mention,
    priority_emoji,
    priority_label,
)

logger = logging.getLogger("tracker")

PLAYERS_GROWTH_THRESHOLD_PERCENT = 20.0
VISITS_GROWTH_THRESHOLD_PERCENT = 5.0


def _compute_delta(old, new):
    """Compute % change. Returns 0 if old is missing or zero."""
    try:
        old_v = float(old or 0)
        new_v = float(new or 0)
    except (TypeError, ValueError):
        return 0.0
    if old_v == 0:
        return 0.0
    return ((new_v - old_v) / old_v) * 100.0


def _should_alert(players_delta, visits_delta):
    """Fire alert if either threshold trips."""
    return (
        players_delta >= PLAYERS_GROWTH_THRESHOLD_PERCENT
        or visits_delta >= VISITS_GROWTH_THRESHOLD_PERCENT
    )


def _classify_tracker_priority(players_delta, visits_delta):
    """Bucket a tracker explosion into high / medium / low.

    Matches the user's "Smart Notification Priority" example:
      • players_delta >= 100%   → high    (BREAKOUT DETECTED)
      • players_delta >= 25%    → medium  (Growing Opportunity)
      • otherwise               → low     (Small Growth, embed-only)
    """
    if players_delta >= 100 or visits_delta >= 50:
        return PRIORITY_HIGH
    if players_delta >= 25 or visits_delta >= 10:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


async def run_tracker(bot):
    """Check all watched games for major stat changes. Returns alert count."""
    logger.info("Tracker starting...")
    start_time = time.time()
    watched = get_all_watched_games()
    save_alert_log("tracker_start", f"Tracking {len(watched)} games")

    if not watched:
        logger.info("No watched games; tracker idle.")
        return 0

    alert_channel = None
    if ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID):
        alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

    tracker_alerts = 0
    error_count = 0

    for (
        user_id,
        game_id,
        game_name,
        last_players,
        last_visits,
        date_added,
    ) in watched:
        try:
            current = await asyncio.to_thread(get_game_info, game_id)
        except Exception as exc:
            error_count += 1
            logger.warning("Tracker fetch failed for %s: %s", game_name, exc)
            continue

        if current is None:
            continue

        try:
            players_delta = _compute_delta(last_players, current["playing"])
            visits_delta = _compute_delta(last_visits, current["visits"])
        except Exception:
            players_delta = visits_delta = 0.0

        # Always update the snapshot so the next tracker compare has fresh numbers,
        # even when no alert fires.
        update_watched_game_snapshot(
            game_id,
            current.get("playing"),
            current.get("visits"),
        )

        if not _should_alert(players_delta, visits_delta):
            continue

        save_alert_log(
            "tracker_alert",
            f"{game_name}: players_delta={players_delta:.1f}%, "
            f"visits_delta={visits_delta:.1f}%",
        )

        if alert_channel is None:
            logger.warning("Tracker had alert but ALERT_CHANNEL_ID unresolved.")
            continue

        try:
            priority = _classify_tracker_priority(players_delta, visits_delta)
            user_setting = get_user_alert_level(user_id)

            embed = create_tracker_embed(
                game_name=game_name,
                old_players=int(last_players or 0),
                new_players=int(current.get("playing", 0)),
                old_visits=int(last_visits or 0),
                new_visits=int(current.get("visits", 0)),
                players_delta=players_delta,
                visits_delta=visits_delta,
                tracked_since=date_added,
                priority=priority,
            )

            # Compose the message body. The "@user_id" mention is conditional
            # on the user's alert_level setting; lower-priority alerts still
            # post so the channel record stays complete, but only HIGH/MEDIUM
            # (per user preference) actually ring the watcher's bell.
            emoji = priority_emoji(priority)
            label = priority_label(priority)

            if should_mention(priority, user_setting):
                body = (
                    f"<@{user_id}> {emoji} **{label}** on **{game_name}** "
                    f"— check the embed below."
                )
            else:
                body = (
                    f"{emoji} _Quietly tracked_ — **{label}** on **{game_name}** "
                    f"({user_setting} alert level skips this)."
                )

            await alert_channel.send(content=body, embed=embed)
            tracker_alerts += 1
            logger.info(
                "Tracker alert: %s priority=%s setting=%s +%.1f%% CCU",
                game_name,
                priority,
                user_setting,
                players_delta,
            )
        except Exception as exc:
            save_alert_log("tracker_post_error", f"{game_name}: {exc}")
            logger.exception("Tracker post failed for %s", game_name)

        # Pace requests to avoid Roblox rate limits
        await asyncio.sleep(0.5)

    seconds = time.time() - start_time
    save_alert_log(
        "tracker_summary",
        f"{len(watched)} watched, {tracker_alerts} alerts, "
        f"{error_count} errors, {seconds:.1f}s",
    )
    logger.info(
        "Tracker done: %d watched, %d alerts, %d errors, %.1fs",
        len(watched),
        tracker_alerts,
        error_count,
        seconds,
    )
    return tracker_alerts
