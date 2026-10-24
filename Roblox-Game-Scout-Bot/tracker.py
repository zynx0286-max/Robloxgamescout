import asyncio
import logging
import time

import chart_rank
from roblox_api import get_game_info
from history import growth_summary as history_growth_summary
from database import (
    get_all_watched_games,
    get_user_alert_level,
    update_watched_game_snapshots_bulk,
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

# A collapse is as actionable as a breakout for an acquisition scout: dying
# momentum can mean a cheap entry — or a reason to walk away. The thresholds
# are deliberately steeper than the growth ones so ordinary noise never
# reads as a decline.
PLAYERS_DECLINE_THRESHOLD_PERCENT = -50.0
VISITS_DECLINE_THRESHOLD_PERCENT = -30.0


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
    """Fire alert if either growth threshold trips."""
    return (
        players_delta >= PLAYERS_GROWTH_THRESHOLD_PERCENT
        or visits_delta >= VISITS_GROWTH_THRESHOLD_PERCENT
    )


def _should_alert_decline(players_delta, visits_delta):
    """Fire alert if either decline threshold trips (collapse of momentum)."""
    return (
        players_delta <= PLAYERS_DECLINE_THRESHOLD_PERCENT
        or visits_delta <= VISITS_DECLINE_THRESHOLD_PERCENT
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


def _classify_decline_priority(players_delta, visits_delta):
    """Bucket a tracker collapse. A halving is never "low" — it is either a
    serious decline (medium) or a outright collapse (high)."""
    if players_delta <= -100 or visits_delta <= -60:
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


def _fetch_growth(game_id):
    """Growth over the window from our own snapshots. {} when unavailable."""
    try:
        return history_growth_summary(game_id)
    except Exception as exc:
        logger.debug("History lookup failed for %s: %s", game_id, exc)
        return {}


async def run_tracker(bot):
    """Check all watched games for major stat changes. Returns alert count."""
    logger.info("Tracker starting...")
    start_time = time.time()
    watched = get_all_watched_games()

    # Group rows by game: several users — plus the bot's own auto-watch — can
    # track the same game, and every row for a game shares one snapshot, so
    # fetching per *row* paid for the same game twice and posted duplicate
    # embeds. One fetch and at most one alert per game per cycle now, with
    # the mention list carrying every eligible human watcher. A source of
    # 'auto' marks a system watch: it posts quietly and never @-mentions.
    tracked: dict[int, dict] = {}
    for (
        row_user_id,
        row_game_id,
        row_game_name,
        row_players,
        row_visits,
        row_added,
        row_source,
    ) in watched:
        entry = tracked.setdefault(
            row_game_id,
            {
                "game_name": row_game_name,
                "last_players": row_players,
                "last_visits": row_visits,
                "date_added": row_added,
                "watchers": [],
                "system": False,
            },
        )
        if row_source == "auto":
            entry["system"] = True
        else:
            entry["watchers"].append(row_user_id)

    save_alert_log("tracker_start", f"Tracking {len(tracked)} games")

    if not tracked:
        logger.info("No watched games; tracker idle.")
        return 0

    # Growth-over-window for every watched game in ONE indexed query
    # (was: one connection + query per game inside the loop, even though
    # growth_summaries_bulk already existed for the scanner).
    from history import growth_summaries_bulk
    try:
        growth_map = growth_summaries_bulk(list(tracked.keys()))
    except Exception:
        logger.exception("Bulk growth read failed; window trend skipped")
        growth_map = {}

    alert_channel = None
    if ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID):
        alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

    # Snapshot writes are batched: one connection + one commit serves the
    # whole cycle (was: a connection and fsync per watched game).
    snapshot_updates: list[tuple] = []

    tracker_alerts = 0
    error_count = 0

    for game_id, entry in tracked.items():
        game_name = entry["game_name"] or f"Game {game_id}"
        last_players = entry["last_players"]
        last_visits = entry["last_visits"]
        date_added = entry["date_added"]
        human_watchers = entry["watchers"]
        system_watch = entry["system"]
        try:
            # get_game_info is natively async — call it directly on the loop
            # (wrapping the coroutine in to_thread would await nothing and
            # raise "coroutine was never awaited" on every watched game).
            current = await get_game_info(game_id)
        except Exception as exc:
            error_count += 1
            logger.warning("Tracker fetch failed for %s: %s", game_name, exc)
            continue

        if current is None:
            continue

        # Skip alerting when the stored snapshot is missing — without a real
        # baseline, delta math would read 0 -> N as a (N/0-style) huge gain
        # and fire a false breakout on the tracker's very first look.
        if last_players in (None, 0) and last_visits in (None, 0):
            logger.debug(
                "Tracker baseline missing for %s; snapshotting only.", game_name
            )
            snapshot_updates.append((
                game_id, current.get("playing"), current.get("visits")
            ))
            continue

        try:
            players_delta = _compute_delta(last_players, current["playing"])
            visits_delta = _compute_delta(last_visits, current["visits"])
        except Exception:
            players_delta = visits_delta = 0.0

        # Always update the snapshot so the next tracker compare has fresh numbers,
        # even when no alert fires. The write itself is batched and flushed
        # after the loop — the in-memory `tracked` map already holds the
        # baseline this cycle compared against.
        snapshot_updates.append((
            game_id, current.get("playing"), current.get("visits")
        ))

        # Growth over the window from our own snapshots: independent
        # confirmation of whether the CCU move is part of a sustained trend
        # or a one-off spike. Read in one bulk pass before the loop; a
        # watched game costs no extra network request and no extra query.
        growth = growth_map.get(game_id) or {}
        ccu_growth_pct = growth.get("ccu_growth_pct")
        history_days = growth.get("history_days", 0)
        if ccu_growth_pct is not None and ccu_growth_pct <= -60:
            save_alert_log(
                "history_decline_warning",
                f"{game_name}: tracked {history_days}d CCU "
                f"{ccu_growth_pct:.1f}% despite "
                f"snapshot delta {players_delta:+.1f}%",
            )

        growth_alert = _should_alert(players_delta, visits_delta)
        decline_alert = _should_alert_decline(players_delta, visits_delta)
        if not growth_alert and not decline_alert:
            continue

        save_alert_log(
            "tracker_alert",
            f"{game_name} ({'decline' if decline_alert else 'growth'}): "
            f"players_delta={players_delta:.1f}%, "
            f"visits_delta={visits_delta:.1f}%",
        )

        if alert_channel is None:
            logger.warning("Tracker had alert but ALERT_CHANNEL_ID unresolved.")
            continue

        try:
            priority = (
                _classify_decline_priority(players_delta, visits_delta)
                if decline_alert
                else _classify_tracker_priority(players_delta, visits_delta)
            )

            # Chart-rank context from the local store: whether the game is
            # currently on a Roblox chart, and where it sits.
            try:
                positions = chart_rank.latest_positions(game_id)
            except Exception:
                positions = []
            chart_summary = " · ".join(
                f"{label} #{pos}" for label, pos in positions[:2]
            ) or None

            # A mention is conditional on each watcher's alert_level; system
            # (auto) watches never appear here, so the bot never @-mentions
            # anyone on its own initiative.
            mention_bits = [
                f"<@{watcher_id}>"
                for watcher_id in human_watchers
                if should_mention(priority, get_user_alert_level(watcher_id))
            ]

            embed = create_tracker_embed(
                game_id=game_id,
                place_id=current.get("place_id"),
                ccu_growth_pct=ccu_growth_pct,
                history_days=history_days,
                game_name=game_name,
                old_players=int(last_players or 0),
                new_players=int(current.get("playing", 0)),
                old_visits=int(last_visits or 0),
                new_visits=int(current.get("visits", 0)),
                players_delta=players_delta,
                visits_delta=visits_delta,
                tracked_since=date_added,
                priority=priority,
                alert_kind="decline" if decline_alert else "growth",
                chart_summary=chart_summary,
            )

            # Compose the message body. Alerts still post for every
            # priority so the channel record stays complete — only the
            # mentions are gated by per-user preference. A system-only
            # watch (the auto-watch feature) posts a distinct quiet line.
            emoji = priority_emoji(priority)
            label = priority_label(priority)

            if mention_bits:
                body = (
                    f"{' '.join(mention_bits[:20])} {emoji} **{label}** "
                    f"on **{game_name}** — check the embed below."
                )
            elif system_watch:
                body = (
                    f"{emoji} _Auto-watched_ — **{label}** on **{game_name}**. "
                    f"The bot is tracking this after its alert; "
                    f"/unwatch {game_id} lifts it."
                )
            else:
                body = (
                    f"{emoji} _Quietly tracked_ — **{label}** on **{game_name}** "
                    f"(watchers' alert levels skip this mention)."
                )

            await alert_channel.send(content=body, embed=embed)
            tracker_alerts += 1
            logger.info(
                "Tracker alert: %s priority=%s watchers=%d system=%s +%.1f%% CCU",
                game_name,
                priority,
                len(human_watchers),
                system_watch,
                players_delta,
            )
        except Exception as exc:
            save_alert_log("tracker_post_error", f"{game_name}: {exc}")
            logger.exception("Tracker post failed for %s", game_name)

        # Pace requests to avoid Roblox rate limits
        await asyncio.sleep(0.5)

    if snapshot_updates:
        try:
            update_watched_game_snapshots_bulk(snapshot_updates)
        except Exception:
            logger.exception(
                "Tracker snapshot flush failed for %d game(s)",
                len(snapshot_updates),
            )

    seconds = time.time() - start_time
    save_alert_log(
        "tracker_summary",
        f"{len(tracked)} watched, {tracker_alerts} alerts, "
        f"{error_count} errors, {seconds:.1f}s",
    )
    logger.info(
        "Tracker done: %d watched, %d alerts, %d errors, %.1fs",
        len(tracked),
        tracker_alerts,
        error_count,
        seconds,
    )
    return tracker_alerts
