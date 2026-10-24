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
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from discord.ext import tasks

from scanner import scan_games_async
from tracker import run_tracker
from ascent import run_ascent_check
from ledger import weekly_ledger_body
from database import (
    auto_watch_game,
    is_alerted,
    mark_alerted,
    save_alert_log,
)
from config import (
    ALERT_CHANNEL_ID,
    ALERT_FILTER_USER_ID,
    ASCENT_CHECK_INTERVAL_MINUTES,
    ASCENT_ENABLED,
    AUTO_WATCH_ALERTS,
    LEDGER_ENABLED,
    LEDGER_HOUR_UTC,
    LEDGER_WEEKDAY,
    SCAN_INTERVAL,
    SCAN_ACTIVE_HOURS,
    SCAN_START_HOUR_ET,
    SCAN_END_HOUR_ET,
    DIGEST_ENABLED,
    DIGEST_HOUR_ET,
    DIGEST_LOOKBACK_HOURS,
    DIGEST_TOP_LIMIT,
)
from embeds import create_alert_embed
from buttons import AlertButtons
from filters import (
    format_rejection_summary,
    passes_alert_filters,
    without_discord,
)
from gemini_analyzer import analyze_game
from history import growth_summary as history_growth_summary
from roblox_api_async import (
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

# Eastern Time handles the EST/EDT daylight-saving switch automatically.
# Fail-soft: if the tz database is somehow missing on the host, fall back
# to server-local time rather than crashing the scheduler import.
try:
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None
    logger.warning(
        "Timezone database unavailable — scan window will use server-local "
        "time. Install the 'tzdata' pip package to fix."
    )

# Remember whether we already logged today's idle notice so the console
# doesn't print "outside scan window" every 30 minutes all night.
_last_idle_log_date = None

# Date of the last posted morning digest (prevents double posts within
# the digest hour, e.g. after a restart at 10:15 ET).
_last_digest_date = None


def _within_active_window(now: Optional[datetime] = None) -> bool:
    """True when scheduled scans are allowed to run right now.

    Window is SCAN_START_HOUR_ET..SCAN_END_HOUR_ET in Eastern Time
    (default 10 AM to 9 PM ET, every day). With SCAN_ACTIVE_HOURS=false
    or a degenerate window (end <= start) the bot scans 24/7.
    """
    if not SCAN_ACTIVE_HOURS or SCAN_END_HOUR_ET <= SCAN_START_HOUR_ET:
        return True
    if now is None:
        now = datetime.now(_ET) if _ET else datetime.now()
    local_now = now.astimezone(_ET) if _ET else now
    return SCAN_START_HOUR_ET <= local_now.hour < SCAN_END_HOUR_ET


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
    """Bucket a discovery-scan match into high / medium / low.

    Base signal: Scout Score + growth, as before. Two momentum modifiers
    then refine it — a game with live breakout characteristics (fresh CCU
    spiking above its recent average, or climbing a Roblox chart across
    cycles) is promoted one tier, while a declining-trend game is demoted
    one. Both signals are already computed by the pipeline (velocity dict
    from the bulk read, chart_rank_delta from the chart_positions store),
    so this costs nothing extra to run.
    """
    scout = game.get("scout_score") or {}
    score = scout.get("total", 0) if isinstance(scout, dict) else 0
    growth = float(game.get("growth", 0) or 0)

    if growth >= 100 or score >= 85:
        tier = PRIORITY_HIGH
    elif growth >= 25 or score >= 70:
        tier = PRIORITY_MEDIUM
    else:
        tier = PRIORITY_LOW

    # Momentum promotion: fresh spike above recent average. Both the
    # numeric ratio and the trend label must agree — the classifier never
    # trusts one field alone, so a hand-built or partial velocity dict
    # can't promote a game on a string by itself.
    velocity = game.get("velocity") or {}
    try:
        spike = float(velocity.get("ccu_spike", 0) or 0)
    except (TypeError, ValueError):
        spike = 0.0
    trending = (
        str(velocity.get("ccu_trend", "")) == "spiking" and spike >= 1.5
    )

    # Momentum promotion: climbing a chart cycle-over-cycle.
    delta = game.get("chart_rank_delta")
    try:
        climbing = delta is not None and int(delta) >= 5
    except (TypeError, ValueError):
        climbing = False

    if (trending or climbing) and tier is not PRIORITY_HIGH:
        tier = PRIORITY_MEDIUM if tier is PRIORITY_LOW else PRIORITY_HIGH

    # Momentum demotion: the 72h trend says the CCU move is a downtrend.
    # Same two-field rule: a real ratio below the stable band required.
    declining = (
        str(velocity.get("ccu_trend", "")) == "declining" and 0 < spike < 0.8
    )
    if declining and tier is not PRIORITY_LOW:
        tier = PRIORITY_LOW if tier is PRIORITY_MEDIUM else PRIORITY_MEDIUM

    return tier


async def _enrich_game_data(
    session: aiohttp.ClientSession,
    game: dict,
) -> dict:
    """
    Enrich a game dict with the per-game data needed for alerts.

    Adds: discord_invite, creator_id/type, and native growth history.

    Ratings, thumbnails, description and creator id/type are NOT fetched
    here — they arrive already attached by the batched pre-pass in
    ``_run_scan_once`` (1 request per 100 games each instead of one per
    game, which is what keeps the host from being rate-limited).

    Growth comes from the bot's own snapshots (``history.py``), not a
    third-party API, so it starts reporting from the second scan cycle a
    game appears in and covers up to 30 days of retained history.
    """
    # Description is carried by the batch payload; kept for safety in
    # case a caller passes a hand-built dict.
    game = await enrich_game_with_description(session, game)

    # Creator details (needed for Discord group links)
    creator_id, creator_type, _ = fetch_creator_details(game)
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

    # Growth over the window, from our own snapshot store. Empty dict means
    # "not enough history yet" (a brand-new game needs two cycles) — the
    # embed simply omits the field rather than claiming 0%.
    try:
        game.update(history_growth_summary(game.get("id", 0)))
    except Exception as exc:
        logger.debug("History lookup failed for %s: %s", game.get("name"), exc)

    return game


async def _post_alert(channel, game):
    """Send a single alert embed with buttons to *channel*.

    Honors per-user preferences:
    - ❌ Reject: users who rejected this game are not pinged (their
      "won't be shown again" confirmation message is now true — the alert
      still posts to the channel, just without their mention).
    - /scanping: only users who opted in via /scanping are mentioned.
    """
    from database import (
        get_pinged_subscribers,
        get_user_alert_level,
        is_user_ignoring,
    )
    from priority import should_mention

    priority = _classify_scan_priority(game)
    embed = create_alert_embed(game, priority=priority)
    view = AlertButtons.with_game(game)

    subscribers = get_pinged_subscribers()
    mention_bits = []
    for user_id in subscribers:
        if is_user_ignoring(user_id, game.get("id", 0)):
            continue
        if should_mention(priority, get_user_alert_level(user_id)):
            mention_bits.append(f"<@{user_id}>")

    content = " ".join(mention_bits[:20]) or None  # cap mentions per alert
    await channel.send(content=content, embed=embed, view=view)
    if not mention_bits:
        logger.info("No eligible ping subscribers for %s", game.get("name"))


async def _run_scan_once(bot):
    """Run a single scan pass and post new opportunities to #alerts."""
    # The before_loop hook only sleeps 5s (it gets no bot argument), so on a
    # slow Discord handshake the first tick could run with channels unresolved
    # and post nothing. This is the authoritative readiness gate.
    await bot.wait_until_ready()
    logger.info("Scheduled scan starting...")
    start_time = time.time()
    scan_count = 0
    matched = 0
    filtered_out = 0
    duplicates = 0
    alerts = 0
    scan_rejections: list[list[str]] = []

    try:
        async with aiohttp.ClientSession() as session:
            results = await scan_games_async(session=session)
            scan_count = len(results)
            save_alert_log("scan_complete", f"Scan completed: {scan_count} games")

            alert_channel = None
            if ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID):
                alert_channel = bot.get_channel(ALERT_CHANNEL_ID)

            # Ratings and thumbnails were already attached in bulk by
            # scan_games_async, before its filters ran — the rating filter
            # rejects games without rating data, so doing it out here meant
            # every candidate had been judged unrated first.

            # Channel-level filters: the ALERT_* base thresholds, plus the
            # /settings profile of ALERT_FILTER_USER_ID when one is set. A
            # shared channel cannot honour every subscriber's personal
            # filters at once, so this is opt-in rather than guessed.
            channel_settings = None
            if ALERT_FILTER_USER_ID:
                try:
                    from database import get_user_filters

                    channel_settings = get_user_filters(ALERT_FILTER_USER_ID)
                    logger.info(
                        "Alert channel filters taken from user %s",
                        ALERT_FILTER_USER_ID,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not load ALERT_FILTER_USER_ID=%s profile: %s",
                        ALERT_FILTER_USER_ID,
                        exc,
                    )

            # Process each game through the enrichment + filter + alert pipeline
            for game in results:
                game_name = game.get("name", f"Game {game.get('id', '?')}")

                # Step 1: Cheap filters first, with the Discord requirement
                # suspended. The Discord lookup costs two extra requests per
                # game to games.roblox.com — the one host that throttles this
                # bot hardest — and a game that fails CCU/visits/rating/age
                # can never alert, so paying for it up front was pure waste.
                pre_passed, pre_failures = passes_alert_filters(
                    game, without_discord(channel_settings)
                )
                if not pre_passed:
                    filtered_out += 1
                    scan_rejections.append(pre_failures)
                    save_alert_log(
                        "filter_rejected",
                        f"{game_name} (id={game.get('id')}): "
                        f"{' | '.join(pre_failures)}",
                    )
                    continue

                # Step 2: Enrich the survivors, then apply the full profile
                # (including the Discord requirement). Filtering still runs
                # BEFORE AI analysis so rejected games never burn the daily
                # AI quota — only alert-worthy games are analyzed.
                try:
                    game = await _enrich_game_data(session, game)
                except Exception as exc:
                    logger.warning("Enrichment failed for %s: %s", game_name, exc)
                    # Continue with whatever data we have

                passed, failures = passes_alert_filters(game, channel_settings)
                if not passed:
                    filtered_out += 1
                    scan_rejections.append(failures)
                    save_alert_log(
                        "filter_rejected",
                        f"{game_name} (id={game.get('id')}): {' | '.join(failures)}",
                    )
                    continue

                matched += 1

                # Step 3: Run AI analysis (cached 24h, fallback if Gemini
                # unavailable). Sync requests+SQLite call — run in a thread
                # so the event loop stays responsive.
                try:
                    if not is_alerted(game["id"]):
                        ai_result = await asyncio.to_thread(
                            analyze_game, game, force_refresh=False
                        )
                        game["ai_analysis"] = ai_result
                except Exception as exc:
                    logger.debug("AI analysis failed for %s: %s", game_name, exc)
                    game["ai_analysis"] = {}

                # Step 4: Check if already alerted
                if is_alerted(game["id"]):
                    duplicates += 1
                    continue

                # Step 5: Post alert. Mark as alerted only AFTER a successful
                # send: marking first meant an unresolved ALERT_CHANNEL_ID or
                # a failed channel.send permanently consumed the game's
                # one-time alert — every later cycle saw it as a duplicate,
                # so a misconfigured channel silently swallowed all matches.
                # A retried game re-pays nothing: the AI result is cached 24h.
                if alert_channel is not None:
                    try:
                        await _post_alert(alert_channel, game)
                        scout_obj = game.get("scout_score") or {}
                        scout_total = (
                            scout_obj.get("total")
                            if isinstance(scout_obj, dict) else None
                        )
                        mark_alerted(game["id"], game["name"], scout_score=scout_total)
                        alerts += 1
                        logger.info("Alert posted: %s (id=%s)", game_name, game["id"])
                        # Auto-watch: a system track so the watched-game
                        # tracker keeps reporting on this game after its
                        # one-time alert. Human watches and their mentions
                        # are untouched; the AUTO_WATCH_MAX cap prunes only
                        # these system rows.
                        if AUTO_WATCH_ALERTS:
                            try:
                                auto_watch_game(game)
                            except Exception as watch_exc:
                                logger.warning(
                                    "Auto-watch failed for %s: %s",
                                    game_name,
                                    watch_exc,
                                )
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

        # Empty-cycle guard: zero matches after filtering is almost always a
        # data bug (enrichment skipped, flags dropped) rather than a dry
        # market — name the dominant rejection category so the culprit is in
        # the very first log you read. Both historic empty-scan bugs would
        # have been caught here on day one.
        if matched == 0 and scan_rejections:
            note = format_rejection_summary(scan_rejections, collected=scan_count)
            logger.warning("Scheduled scan matched 0 games — %s", note)
            save_alert_log("rejection_summary", note)

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
    """Start the scheduled scan and digest loops on the given bot instance."""
    if not scheduled_scan.is_running():
        scheduled_scan.start(bot)
        logger.info("Scheduled scan task started (every %s minutes).", SCAN_INTERVAL)
    if DIGEST_ENABLED and not morning_digest.is_running():
        morning_digest.start(bot)
        logger.info(
            "Morning digest task started (daily at %02d:00 ET).", DIGEST_HOUR_ET
        )
    if LEDGER_ENABLED and not weekly_ledger.is_running():
        weekly_ledger.start(bot)
        logger.info(
            "Weekly ledger task started (day %d at %02d:00 UTC).",
            LEDGER_WEEKDAY,
            LEDGER_HOUR_UTC,
        )


def stop_scheduler():
    """Cancel the scheduled scan, digest and ascent-radar tasks if they are running."""
    if scheduled_scan.is_running():
        scheduled_scan.cancel()
        logger.info("Scheduled scan task cancelled.")
    if morning_digest.is_running():
        morning_digest.cancel()
        logger.info("Morning digest task cancelled.")
    if ascent_radar.is_running():
        ascent_radar.cancel()
        logger.info("Ascent radar task cancelled.")
    if weekly_ledger.is_running():
        weekly_ledger.cancel()
        logger.info("Weekly ledger task cancelled.")


@tasks.loop(minutes=ASCENT_CHECK_INTERVAL_MINUTES)
async def ascent_radar(bot):
    """Ascent Radar: catch breakouts between scan cycles.

    A light, local-only pass: the radar reads the bot's own game_history
    snapshots (no Roblox requests) and posts a short alert when a game's
    fresh CCU reading is out of line with its recent average. Detection,
    cooldown, per-cycle cap and the alerted-games escalation rule all live
    in ascent.py; the tick itself just runs and logs.
    """
    if not ASCENT_ENABLED:
        return
    await bot.wait_until_ready()
    try:
        posted = await run_ascent_check(bot)
        if posted:
            logger.info("Ascent radar posted %d alert(s).", posted)
    except Exception:
        logger.exception("Ascent radar tick failed")


@ascent_radar.before_loop
async def _before_ascent_radar():
    """Settle briefly like the other loops; the body waits for full readiness."""
    await asyncio.sleep(5)


@ascent_radar.error
async def _ascent_radar_error(exc):
    logger.exception("Ascent radar loop error: %s", exc)


@tasks.loop(hours=1)
async def weekly_ledger(bot):
    """Hourly tick that posts the Weekly Ledger once, on its day+hour.

    The body itself gates on weekday/hour/last-post-date, so restarts
    cannot double-post and a misconfigured channel never eats the post.
    """
    await bot.wait_until_ready()
    try:
        posted = await weekly_ledger_body(bot)
        if posted:
            logger.info("Weekly ledger posted.")
    except Exception:
        logger.exception("Weekly ledger tick failed")


@weekly_ledger.before_loop
async def _before_weekly_ledger():
    """Settle briefly like the other loops; the body waits for readiness."""
    await asyncio.sleep(5)


@weekly_ledger.error
async def _weekly_ledger_error(exc):
    logger.exception("Weekly ledger loop error: %s", exc)


@tasks.loop(minutes=SCAN_INTERVAL)
async def scheduled_scan(bot):
    """Background loop that runs every SCAN_INTERVAL minutes.

    Scans only inside the configured ET window (default 10 AM–9 PM ET,
    every day); outside the window the tick idles and waits for the next
    one so nothing is scanned or posted overnight.
    """
    if not _within_active_window():
        global _last_idle_log_date
        now = datetime.now(_ET) if _ET else datetime.now()
        today = now.date()
        if today != _last_idle_log_date:
            _last_idle_log_date = today
            logger.info(
                "Outside scan window (%02d:00–%02d:00 ET) — sleeping until "
                "the window opens.",
                SCAN_START_HOUR_ET,
                SCAN_END_HOUR_ET,
            )
        return
    await _run_scan_once(bot)


@scheduled_scan.before_loop
async def _before_scheduled_scan():
    """Settle briefly; the real readiness wait happens in _run_scan_once,
    which holds the bot instance (before_loop hooks receive no arguments)."""
    await asyncio.sleep(5)


@scheduled_scan.error
async def _scheduled_scan_error(exc):
    """Log errors raised by the loop so they never silently disappear."""
    logger.exception("Scheduled scan loop error: %s", exc)


# ---------------------------------------------------------------------------
# Morning digest — one summary message per day when the scan window opens.
# ---------------------------------------------------------------------------

def _local_et_now():
    """Current wall-clock time in ET (or server-local if tz data missing)."""
    return datetime.now(_ET) if _ET else datetime.now()


def build_digest_message(
    alerted_rows=None,
    alerts_count=None,
    cycles_count=None,
    now=None,
    leaderboard_rows=None,
):
    """Render the daily digest text. Pure function — easy to test.

    alerted_rows: rows of (game_name, first_alerted) from
    database.get_alerted_games_since().
    leaderboard_rows: optional pre-fetched top_alerted_by_scout_score()
    rows — pass them to test the render without a store; None reads the
    live store lazily, exactly like every other section.
    """
    import database as _db

    now = now or _local_et_now()
    if alerted_rows is None:
        alerted_rows = _db.get_alerted_games_since(
            hours=DIGEST_LOOKBACK_HOURS, limit=DIGEST_TOP_LIMIT
        )
    if alerts_count is None:
        alerts_count = _db.count_alerted_since(hours=DIGEST_LOOKBACK_HOURS)
    if cycles_count is None:
        cycles_count = _db.count_scan_cycles_since(hours=DIGEST_LOOKBACK_HOURS)

    window = (
        f"{SCAN_START_HOUR_ET}:00–{SCAN_END_HOUR_ET}:00 ET"
        if (SCAN_ACTIVE_HOURS and SCAN_END_HOUR_ET > SCAN_START_HOUR_ET)
        else "24/7"
    )

    lines = [
        "☀️ **Good morning — Scout daily digest**",
        f"Window: {window} · Scan every {SCAN_INTERVAL} min",
        f"Scans completed: **{cycles_count}** · New games alerted: **{alerts_count}**",
    ]

    if alerted_rows:
        lines.append("")
        lines.append("**New since yesterday:**")
        for name, ts in alerted_rows:
            day = str(ts)[:10] if ts else "?"
            lines.append(f"• {name} — _{day}_")
    else:
        lines.append("")
        lines.append(
            "No new games passed the filters in the last "
            f"{DIGEST_LOOKBACK_HOURS}h. The scout keeps looking today."
        )

    # Pulse section: what the bot is seeing right now, from its own local
    # data (no Roblox requests). Fail-soft — a corrupt table can never eat
    # the digest.
    try:
        from pulse import (
            PULSE_WINDOW_HOURS,
            recent_ascent_events,
            render_ascents,
            render_climbers,
            render_movers,
            top_chart_climbers,
            top_movers,
        )

        movers = top_movers()
        climbers = top_chart_climbers()
        ascents = recent_ascent_events()

        lines.append("")
        lines.append(f"📊 **Pulse — last {PULSE_WINDOW_HOURS}h**")

        mover_lines = render_movers(movers)
        lines.append("**CCU movers:**")
        if mover_lines:
            lines.extend(mover_lines)
        else:
            lines.append("_No fresh CCU movement yet._")

        climber_lines = render_climbers(climbers)
        lines.append("**Chart climbers:**")
        if climber_lines:
            lines.extend(climber_lines)
        else:
            lines.append("_No chart climbs recorded yet._")

        ascent_lines = render_ascents(ascents)
        lines.append("**Ascent radar:**")
        if ascent_lines:
            lines.extend(ascent_lines)
        else:
            lines.append("_Radar quiet — no qualifying breakouts._")
    except Exception as exc:
        lines.append("")
        lines.append("📊 _Pulse section unavailable: %s_" % exc)

    # Leaderboard: the scout's track record (all-time best Scout Scores
    # among alerted games). Same store / same data as /leaderboard, zero
    # Roblox requests. Fail-soft like the pulse section; an empty store
    # renders nothing — the digest above already carries the day's story.
    try:
        if leaderboard_rows is None:
            leaderboard_rows = _db.top_alerted_by_scout_score(limit=5, days=0)
        leaderboard_lines = _render_leaderboard_lines(leaderboard_rows)
    except Exception as exc:
        leaderboard_lines = ["_Leaderboard section unavailable: %s_" % exc]
    if leaderboard_lines:
        lines.append("")
        lines.append("🏁 **Scout leaderboard — all time**")
        lines.extend(leaderboard_lines)

    return "\n".join(lines)


def _render_leaderboard_lines(rows):
    """Leaderboard lines from store rows; returns [] on an empty store."""
    lines = []
    for rank, (gid, gname, score, count, _first) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")
        score_text = f"`{score}/100`" if score is not None else "`—`"
        repeat = " · seen " + str(count) if (count or 1) > 1 else ""
        lines.append(f"{medal} {gname or f'ID {gid}'} — {score_text}{repeat}")
    return lines


async def post_morning_digest(bot):
    """Post the daily digest to the alert channel. Fail-soft."""
    if not (ALERT_CHANNEL_ID and bot.get_channel(ALERT_CHANNEL_ID)):
        logger.warning(
            "Morning digest skipped: ALERT_CHANNEL_ID=%s not resolved.",
            ALERT_CHANNEL_ID,
        )
        return
    try:
        channel = bot.get_channel(ALERT_CHANNEL_ID)
        text = build_digest_message()
        await channel.send(text)
        logger.info("Morning digest posted.")
    except Exception:
        logger.exception("Morning digest failed")


@tasks.loop(hours=1)
async def morning_digest(bot):
    """Hourly tick that posts the digest once per day at DIGEST_HOUR_ET."""
    await bot.wait_until_ready()
    global _last_digest_date
    now = _local_et_now()
    if now.hour != DIGEST_HOUR_ET:
        return
    today = now.date()
    if today == _last_digest_date:
        return
    _last_digest_date = today
    await post_morning_digest(bot)


@morning_digest.before_loop
async def _before_morning_digest():
    """Settle briefly; the digest body itself waits for full readiness."""
    await asyncio.sleep(5)


@morning_digest.error
async def _morning_digest_error(exc):
    logger.exception("Morning digest loop error: %s", exc)