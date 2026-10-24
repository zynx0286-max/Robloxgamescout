import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from database import (
    add_user,
    get_user_filters,
    get_user_row,
    get_user_alert_level,
    update_user,
    get_saved_games_for_user,
    get_watched_games_for_user,
    get_ignored_games_for_user,
    unwatch_game_for_user,
    unignore_game_for_user,
    is_user_watching,
    get_pinged_subscribers,
)
from priority import (
    SETTING_ALL,
    SETTING_MEDIUM_PLUS,
    SETTING_HIGH_ONLY,
    normalize_setting,
)

logger = logging.getLogger("commands")


# One full scan at a time, process-wide. Two overlapping scans double the
# load on games.roblox.com (the host that throttles hardest) and the pacer
# makes both slower; the scheduler and /scan share this guard.
_scan_lock = asyncio.Lock()


_ALERT_LEVEL_CHOICES = [
    app_commands.Choice(
        name="All — mention me on every alert",
        value=SETTING_ALL,
    ),
    app_commands.Choice(
        name="Medium+ — skip low priority",
        value=SETTING_MEDIUM_PLUS,
    ),
    app_commands.Choice(
        name="High Only — only breakouts ring the bell",
        value=SETTING_HIGH_ONLY,
    ),
]


# Human-readable label so /profile and /settings echo something readable.
_PROMPT_ALERT_LEVEL = {
    SETTING_ALL: "All",
    SETTING_MEDIUM_PLUS: "Medium+",
    SETTING_HIGH_ONLY: "High Only",
}
import aiohttp

from config import BOT_VERSION
from roblox_api import get_game_info
from scanner import calculate_score, scan_games, scan_games_async, calculate_scout_score
from growth import get_growth
from trend import calculate_trend_score
from categories import get_category
from embeds import create_game_embed
from buttons import AlertButtons
from gemini_analyzer import (
    analyze_game,
    format_report,
    get_daily_limit,
    get_usage_today,
    quota_remaining,
    provider_status,
)


def _reasons(game):
    """Build a list of positive signals for a game."""
    reasons = []
    if game.get("growth", 0) >= 50:
        reasons.append("✅ Fast growth")
    if game.get("trend_score", 0) >= 80:
        reasons.append("✅ Trending strongly")
    if game["playing"] >= 1000:
        reasons.append("✅ High activity")
    if game["visits"] >= 1000000:
        reasons.append("✅ Strong engagement")
    if game["favorites"] >= 10000:
        reasons.append("✅ Popular with players")
    if not reasons:
        reasons.append("ℹ️ Low activity right now")
    return "\n".join(reasons)


def _format_game_message(index, game):
    """Format a single game result message (fallback text version)."""
    growth = game.get("growth", 0)
    growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"
    category = get_category(game)

    return (
        f"**{index}. {game['name']}**\n\n"
        f"👥 Players:\n{game['playing']:,}\n\n"
        f"📈 Growth:\n{growth_text}\n\n"
        f"🔥 Trend Score:\n{game.get('trend_score', 0)}/100\n\n"
        f"⭐ Category:\n{category}\n\n"
        f"📝 Why:\n{_reasons(game)}"
    )


def setup_commands(tree: app_commands.CommandTree):
    """Register all slash commands with the bot's command tree."""

    @tree.command(name="hello", description="Check if the bot is working")
    async def hello(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(
            "Roblox Game Scout Bot is online!"
        )

    @tree.command(
        name="testbuttons",
        description="Post a single test embed with the alert buttons"
    )
    async def testbuttons(interaction: discord.Interaction):
        from embeds import create_alert_embed

        sample_game = {
            "id": 994732206,
            "place_id": 2753915549,
            "name": "Blox Fruits (Test Alert)",
            "playing": 280000,
            "visits": 62900000000,
            "favorites": 19000000,
            "growth": 12.5,
            "score": 70,
            "trend_score": 25,
            "creator": "Gamer Robot Inc",
            "source": "Test",
        }

        embed = create_alert_embed(sample_game)
        await interaction.response.send_message(
            embed=embed,
            view=AlertButtons.with_game(sample_game),
        )

    @tree.command(
        name="testtracker",
        description="Simulate a +50% CCU alert for a tracked game (dev only)"
    )
    async def testtracker(interaction: discord.Interaction):
        from embeds import create_tracker_embed

        embed = create_tracker_embed(
            game_name="Blox Fruits (Simulated)",
            old_players=200000,
            new_players=300000,
            old_visits=62000000000,
            new_visits=62500000000,
            players_delta=50.0,
            visits_delta=0.81,
            tracked_since="2026-07-27",
        )
        await interaction.response.send_message(
            "🧪 **Simulated tracker alert** — proving the embed layout works:",
            embed=embed,
        )

    @tree.command(
        name="status",
        description="Show the result of the startup dependency health check",
    )
    async def status(interaction: discord.Interaction):
        import health_check

        await interaction.response.defer(thinking=True)
        # Re-run the probes live so /status reflects reality right now,
        # not just what boot-time saw. Fail-soft on any probe error.
        try:
            results = await health_check.run_startup_health_check()
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Health check failed to run: {exc}", ephemeral=True
            )
            return
        await interaction.followup.send(
            health_check.format_health_results(results)
        )

    @tree.command(
        name="scanping",
        description="Toggle pings on scheduled discovery alerts",
    )
    async def scanping(interaction: discord.Interaction):
        user_id = interaction.user.id
        add_user(user_id)
        current = get_user_filters(user_id).get("ping_subscribed", False)
        new_value = not current
        update_user(user_id, ping_subscribed=new_value)
        if new_value:
            await interaction.response.send_message(
                "🔔 You will now be **pinged** when a scheduled scan posts a "
                "discovery alert. Use `/scanping` again to turn pings off.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🔕 Discovery-alert pings **off**. You'll still see alerts in "
                "the alert channel.",
                ephemeral=True,
            )

    @tree.command(
        name="scanwindow",
        description="Show the automated scan schedule (interval + active hours)",
    )
    async def scanwindow(interaction: discord.Interaction):
        import scheduler as _sched
        from config import (
            DIGEST_ENABLED,
            DIGEST_HOUR_ET,
            SCAN_ACTIVE_HOURS,
            SCAN_END_HOUR_ET,
            SCAN_INTERVAL,
            SCAN_START_HOUR_ET,
        )

        inside = _sched._within_active_window()
        if SCAN_ACTIVE_HOURS and SCAN_END_HOUR_ET > SCAN_START_HOUR_ET:
            window_txt = f"{SCAN_START_HOUR_ET}:00–{SCAN_END_HOUR_ET}:00 ET"
            status_txt = (
                f"🟢 Active — scanning until {SCAN_END_HOUR_ET}:00 ET"
                if inside
                else f"🌙 Outside window — scans resume at {SCAN_START_HOUR_ET}:00 ET"
            )
        else:
            window_txt = "24/7 (no time window)"
            status_txt = "🟢 Active"

        tz_txt = (
            "Eastern Time (auto DST)" if _sched._ET else "server time (tz data missing)"
        )
        digest_txt = f"Daily at {DIGEST_HOUR_ET}:00 ET" if DIGEST_ENABLED else "Off"

        await interaction.response.send_message(
            "⏱ **Automated scan schedule**\n\n"
            f"**Interval:** every {SCAN_INTERVAL} minutes\n"
            f"**Active window:** {window_txt}\n"
            f"**Status:** {status_txt}\n"
            f"**Morning digest:** {digest_txt}\n"
            f"**Clock used:** {tz_txt}\n\n"
            "_Use `/scan-now` to force a scan immediately._"
        )

    @tree.command(
        name="scan-now",
        description="Force an opportunity scan right now (ignores the schedule window)",
    )
    async def scan_now(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        from scheduler import _run_scan_once

        await interaction.followup.send(
            "🔎 Manual scan started — this can take up to a minute. "
            "New alerts are posted to the alert channel."
        )
        try:
            await _run_scan_once(interaction.client)
            await interaction.followup.send(
                "✅ Manual scan finished. Check the alert channel for any new "
                "opportunities (games already alerted before are not repeated)."
            )
        except Exception as exc:
            await interaction.followup.send(
                f"⚠️ Manual scan failed: `{exc}`"
            )

    @tree.command(
        name="tokencheck",
        description="Show which environment variables the host actually passed in",
    )
    async def tokencheck(interaction: discord.Interaction):
        # Existence check only. Secret values are never printed back —
        # only whether the variable is set and how long it is, which is
        # enough to debug "the panel wiped my variables" problems without
        # leaking a token into a Discord channel.
        from config import DATABASE_PATH

        def _state(value):
            if not value:
                return "❌ MISSING"
            return f"✅ set ({len(str(value))} chars)"

        # Which provider answers AI calls, and what happens if it is down.
        try:
            import gemini_analyzer as _ga
            ai_line = _ga.provider_status()
        except Exception as exc:
            ai_line = f"unavailable ({exc})"

        token = os.getenv("DISCORD_TOKEN")
        lines = [
            "🔧 **Host configuration check**",
            "",
            f"`DISCORD_TOKEN` : {_state(token)}",
            f"`GUILD_ID` : {_state(os.getenv('GUILD_ID'))}",
            f"`ALERT_CHANNEL_ID` : {_state(os.getenv('ALERT_CHANNEL_ID'))}",
            f"`GROQ_API_KEY` : {_state(os.getenv('GROQ_API_KEY'))}",
            f"`GEMINI_API_KEY` : {_state(os.getenv('GEMINI_API_KEY'))}",
            "",
            f"**AI provider order:** {ai_line}",
            "",
            "**Runtime**",
            f"Python : `{sys.version.split()[0]}`",
            f"Working dir : `{os.getcwd()}`",
            f"Database : `{DATABASE_PATH}` "
            f"({'exists' if os.path.exists(DATABASE_PATH) else 'not created yet'})",
            f"`.env` file : "
            f"{'found in working dir' if os.path.exists('.env') else 'not here'}",
        ]

        if not token:
            lines += [
                "",
                "⚠️ Without `DISCORD_TOKEN` the bot cannot log in at all. "
                "Add it in the host panel's Startup / Environment tab (or a "
                "`.env` file next to `main.py`), then restart the server.",
            ]
        elif not os.getenv("ALERT_CHANNEL_ID"):
            lines += [
                "",
                "⚠️ `ALERT_CHANNEL_ID` is missing, so scheduled scans will "
                "have nowhere to post alerts.",
            ]
        elif not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):
            lines += [
                "",
                "⚠️ No AI provider key, so every game gets a data-only "
                "verdict. Add `GROQ_API_KEY` (free, console.groq.com) and/or "
                "`GEMINI_API_KEY` — with both set, one backs up the other.",
            ]
        else:
            lines += ["", "✅ Configuration looks complete."]

        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    @tree.command(
        name="scorecard",
        description="How the bot's own alert calls have actually performed",
    )
    async def scorecard(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        from scorecard import scorecard_report

        await interaction.followup.send(scorecard_report())

    @tree.command(
        name="pulse",
        description="What the bot is seeing right now: movers, chart climbers, radar catches",
    )
    async def pulse(interaction: discord.Interaction):
        # Instant local reads — defer only to stay safe against any brief
        # SQLite contention.
        await interaction.response.defer(thinking=True)
        from pulse import pulse_report

        await interaction.followup.send(pulse_report())

    @tree.command(
        name="radar",
        description="Ascent radar diagnostics: coverage counts and current breakout candidates",
    )
    async def radar(interaction: discord.Interaction):
        # Same contract as /pulse: pure local reads, defer to be safe
        # against SQLite contention.
        await interaction.response.defer(thinking=True)
        from ascent import radar_status

        await interaction.followup.send(radar_status())

    _bench_lock = asyncio.Lock()
    _bench_cached_at = 0.0
    _bench_cached_text = ""

    @tree.command(
        name="benchmark",
        description="Measure the bot's bulk-read performance on a throwaway synthetic store",
    )
    async def benchmark(interaction: discord.Interaction):
        """Run scripts/bench_bulk_reads.py in a SUBPROCESS.

        Subprocess isolation matters: the bench mutates the module-level
        database.DATABASE to point at its throwaway store, which inside
        this process would momentarily redirect a live scan. The result is
        cached process-wide for 10 minutes — repeated calls replay it
        instead of re-burning CPU while other commands wait.

        The post carries the drift/trend verdict for THIS host and, when
        the log holds more than one machine, the same per-machine table
        /perfhistory shows (each host's last run, evidence and verdict),
        so an alert on a machine that has not run recently is not lost.
        """
        import time as _time

        nonlocal _bench_cached_at, _bench_cached_text
        await interaction.response.defer(thinking=True)
        try:
            async with _bench_lock:  # one bench at a time, too
                now = _time.monotonic()
                if (now - _bench_cached_at) > 600:
                    script = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "scripts", "bench_bulk_reads.py",
                    )
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=os.path.dirname(script),
                    )
                    try:
                        out, err = await asyncio.wait_for(
                            proc.communicate(), timeout=120
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await interaction.followup.send(
                            "⏱ **Benchmark timed out after 120s** — nothing posted."
                        )
                        return
                    if proc.returncode != 0:
                        err_text = err.decode(errors="replace")[-500:]
                        await interaction.followup.send(
                            "⚠️ **Benchmark failed** — exit "
                            + str(proc.returncode) + "@ ``"
                            + (err_text or "no stderr") + "``"
                        )
                        return
                    _bench_cached_text = out.decode(errors="replace").strip()
                    _bench_cached_at = now
            # Drift check: a deployment box running far below the committed
            # per-phase speedups means the shipped baseline no longer
            # represents this machine — surface it right in the post.
            from bench_guard import (bench_get_drift_lines, bench_get_trend_lines,
                                     bench_machine_lines)

            drift_lines = bench_get_drift_lines(_bench_cached_text)
            body = (
                "⏱ **Bulk-read benchmark** (synthetic store, two measured passes):\n"
                "```\n"
                + _bench_cached_text
                + "\n```"
            )
            if drift_lines:
                body += "\n" + "\n".join(drift_lines)
                logger.warning(
                    "Benchmark drift vs committed baseline: %d drift "
                    "line(s) (phases and/or a host-wide slide)",
                    len(drift_lines),
                )
            # Trend check: the bench run itself just appended its dated
            # entry to perf_history.jsonl; this reads the accumulated log
            # and flags phases that drifted repeatedly in recent runs —
            # the "one noisy run is noise, a string of them is a trend"
            # memory on top of the per-run drift check above.
            trend_lines = bench_get_trend_lines(_bench_cached_text)
            if trend_lines:
                body += "\n" + "\n".join(trend_lines)
            # Per-machine table: this run was measured on ONE host, so
            # the lines above are about that host alone. With more than
            # one machine in the log, post each machine's last verdict
            # (and the evidence behind it) so the channel also carries
            # the alert on a host that has not run recently — same table
            # /perfhistory shows, from the same renderer.
            machine_lines = bench_machine_lines()
            if machine_lines:
                body += "\n\n" + "\n".join(machine_lines)
            await interaction.followup.send(body)
        except Exception as exc:
            logger.exception("Benchmark command failed")
            await interaction.followup.send(f"⚠️ Benchmark failed: {exc}")

    @tree.command(
        name="perfhistory",
        description="Show the accumulated bench-drift history — no benchmark run",
    )
    async def perfhistory(interaction: discord.Interaction):
        """Read the perf_history.jsonl trend WITHOUT re-running the bench.

        /benchmark measures (and pays CPU); this command only reads the
        log the bench already wrote: recent runs, per-phase ratios vs the
        committed baseline, one row per MACHINE when the log spans more
        than one (its last run, the evidence behind it — implicated
        phase and how many of its own recent runs were low — and the
        verdict it got then: a host that has not run since the runs
        moved elsewhere would otherwise look resolved), and the SAME
        routed action /benchmark posts for the
        latest run — fix the regressed read path (or "the alert is
        stuck: the fix is not holding" once the window is saturated),
        rerun, suspect host noise, or rule out the host on a shared slide
        — one story, zero measurement cost.
        """
        from bench_guard import bench_read_history_view

        view = bench_read_history_view(limit=8)
        if view is None:
            await interaction.response.send_message(
                "⚠️ **Perf history unavailable** — the read-only bridge to "
                "scripts/bench_bulk_reads.py failed (fail-soft: nothing "
                "was run and nothing was written)."
            )
            return
        if view.startswith("No bench runs"):
            await interaction.response.send_message(f"📚 {view}")
            return
        await interaction.response.send_message(
            "📚 **Perf history** (read-only — no bench was run):\n"
            "```\n" + view + "\n```"
        )

    @tree.command(name="about", description="Learn what this bot does")
    async def about(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🎮 **Roblox Game Scout Bot** v{BOT_VERSION}\n\n"
            "A tool that finds Roblox games with:\n"
            "• High growth potential\n"
            "• Trending activity\n"
            "• Strong player numbers\n"
            "• Developer opportunities\n\n"
            "It also shows its work: `/pulse` (market read), "
            "`/dossier` (one-game drill-in), "
            "`/scorecard` (how its own calls performed), plus a "
            "weekly **Ledger** posted to the alert channel — the bot "
            "grading its own alerts and AI verdicts, unprompted."
        )

    @tree.command(name="profile", description="View your scout settings")
    async def profile(interaction: discord.Interaction):
        user_id = interaction.user.id

        add_user(user_id)
        f = get_user_filters(user_id)

        if not f:
            await interaction.response.send_message(
                "⚙️ No profile yet — set filters with **/settings**."
            )
            return

        f["alert_level"] = (get_user_alert_level(user_id) or "all").lower()
        f["min_visits"] = f.get("minimum_visits", 0)
        f["max_visits"] = f.get("maximum_visits", 0)
        f["min_players"] = f.get("minimum_players", 0)
        f["max_players"] = f.get("maximum_players", 0)
        f["min_growth"] = f.get("minimum_growth", 0)
        f["min_rating"] = f.get("minimum_rating", 75)
        f["require_discord"] = f.get("require_discord", True)
        f["require_charts"] = f.get("require_charts", False)
        f["genre"] = f.get("genre", "Any")
        f["max_age"] = f.get("max_age", 365)

        def _v(x):
            return "— (no cap)" if x == 0 else f"{x:,}"

        await interaction.response.send_message(
            f"""
🎮 Scout Profile

CCU:
{f['min_players']:,} - {_v(f['max_players'])}

Visits:
{f['min_visits']:,} - {_v(f['max_visits'])}

Rating:
{f['min_rating']}%+

Discord Required:
{'Yes' if f['require_discord'] else 'No'}

Roblox Chart Presence Required:
{'Yes' if f['require_charts'] else 'No'}

Minimum Growth:
{f['min_growth']}%

Genre Filter:
{f['genre']}

Max Game Age:
{'— (no cap)' if not f['max_age'] else str(f['max_age']) + ' days'}

🔔 Alert Level:
{_PROMPT_ALERT_LEVEL.get(f['alert_level'], f['alert_level'])}
"""
        )

    @tree.command(
        name="settings",
        description="Change your scout filters (any field can be omitted)",
    )
    @app_commands.describe(
        min_visits="Minimum visits (default 0)",
        max_visits="Maximum visits before filtering out (default 1,500,000)",
        min_players="Minimum current CCU (default 15)",
        max_players="Maximum current CCU (default 2,500)",
        min_growth="Minimum growth percentage (default 0)",
        genre="Genre keyword to filter by (Simulator, RPG, Tycoon, Any…)",
        max_age="Maximum age in days, 0 = no cap (games with an unknown date always pass)",
        min_rating="Minimum rating percent (default 75, 0 = no rating requirement)",
        require_discord="Require a Discord invite on scanned games (default No — invites are only detected in the game description)",
        require_charts="Only alert on games currently on a Roblox chart (default No)",
        alert_level="How loud alerts are. Quiet low alerts aren't mentioned.",
    )
    @app_commands.choices(
        alert_level=_ALERT_LEVEL_CHOICES,
        require_discord=[
            app_commands.Choice(name="Yes", value=1),
            app_commands.Choice(name="No", value=0),
        ],
        require_charts=[
            app_commands.Choice(name="Yes", value=1),
            app_commands.Choice(name="No", value=0),
        ],
    )
    async def settings(
        interaction: discord.Interaction,
        min_visits: int = None,
        max_visits: int = None,
        min_players: int = None,
        max_players: int = None,
        min_growth: int = None,
        genre: str = None,
        max_age: int = None,
        min_rating: int = None,
        require_discord: int = None,
        require_charts: int = None,
        alert_level: str = None,
    ):
        user_id = interaction.user.id

        add_user(user_id)

        # Normalize before persisting so the stored value matches priority.py.
        canonical_alert_level = (
            normalize_setting(alert_level) if alert_level is not None else None
        )

        update_user(
            user_id,
            minimum_visits=min_visits,
            maximum_visits=max_visits,
            minimum_players=min_players,
            maximum_players=max_players,
            minimum_growth=min_growth,
            genre=genre,
            max_age=max_age,
            alert_level=canonical_alert_level,
            minimum_rating=min_rating,
            require_discord=require_discord,
            require_charts=require_charts,
        )

        f = get_user_filters(user_id)

        def _v(x):
            return "— (no cap)" if x == 0 else f"{x:,}"

        await interaction.response.send_message(
            f"""
⚙️ Scout Settings Updated

CCU: {f['minimum_players']:,} - {_v(f['maximum_players'])}
Visits: {f['minimum_visits']:,} - {_v(f['maximum_visits'])}
Rating: {f['minimum_rating']}%+
Discord Required: {'Yes' if f['require_discord'] else 'No'}
Roblox Chart Required: {'Yes' if f['require_charts'] else 'No'}

Minimum Growth: {f['minimum_growth']}%
Genre Filter: {f['genre']}
Max Game Age: {'— (no cap)' if not f['max_age'] else str(f['max_age']) + ' days'}

🔔 Alert Level: {_PROMPT_ALERT_LEVEL.get(f['alert_level'], f['alert_level'])}
""".strip()
        )

    @tree.command(
        name="dossier",
        description="Full briefing on one game: live stats, history, charts, trackers, verdicts",
    )
    @app_commands.describe(game_id="Roblox game Universe ID")
    async def dossier(interaction: discord.Interaction, game_id: str):
        await interaction.response.defer(thinking=True)

        from dossier import build_dossier_view

        try:
            gid = int(game_id)
        except ValueError:
            await interaction.followup.send(
                "That doesn't look like a game ID — pass the numeric "
                "Universe ID (right-click a game's /game output to copy it)."
            )
            return

        report = await build_dossier_view(gid)
        await interaction.followup.send(report)

    @tree.command(name="game", description="Get Roblox game information")
    @app_commands.describe(place_id="Roblox game Universe ID")
    async def game(interaction: discord.Interaction, place_id: str):
        await interaction.response.defer(thinking=True)

        # get_game_info is natively async — call it directly on the loop.
        data = await get_game_info(place_id)

        if data is None:
            await interaction.followup.send(
                "Could not find that Roblox game."
            )
            return

        await interaction.followup.send(
            f"""
🎮 {data['name']}

👥 Players:
{data['playing']:,}

👀 Visits:
{data['visits']:,}

⭐ Favorites:
{data['favorites']:,}

👤 Creator:
{data['creator']}
"""
        )

    @tree.command(name="scoretest", description="Test the game scoring system")
    async def scoretest(interaction: discord.Interaction):
        game = {
            "playing": 5000,
            "visits": 5000000,
            "favorites": 100000
        }

        score = calculate_score(game)

        await interaction.response.send_message(
            f"Game Scout Score: {score}/100"
        )

    @tree.command(
        name="testai",
        description="Run the AI analyst on sample game data (read-only)"
    )
    async def testai(interaction: discord.Interaction):
        sample_game = {
            "id": 994732206,
            "place_id": 2753915549,
            "name": "Blox Fruits (Test AI)",
            "playing": 280000,
            "visits": 62900000000,
            "favorites": 19000000,
            "growth": 12.5,
            "creator": "Gamer Robot Inc",
            "source": "Test",
            "created": "2024-09-15T00:00:00Z",
            "scout_score": {
                "total": 67,
                "verdict": "🔥 Strong opportunity",
                "breakdown": [
                    ("📈 Growth", 25, "🚀 Strong growth"),
                    ("👥 Player momentum", 25, "🏟 Stadium-scale"),
                    ("🆕 New release", 0, "⏳ Mature game"),
                    ("❤️ Like ratio", 7, "❤️ High favor rate"),
                    ("👨\u200d💻 Developer history", 10, "Verified creator"),
                ],
            },
            "trend_score": 25,
        }

        await interaction.response.defer(thinking=True)
        # Sync Gemini call (requests + SQLite) — run in a thread so the
        # event loop stays responsive while waiting on HTTP.
        analysis = await asyncio.to_thread(
            analyze_game, sample_game, force_refresh=False
        )
        report = format_report(sample_game["name"], analysis)
        await interaction.followup.send(report)

    @tree.command(
        name="analyze",
        description="Run the AI analyst on a specific Roblox game by universe ID",
    )
    @app_commands.describe(
        place_id="Roblox game Universe ID, e.g. 994732206 for Blox Fruits",
        force_refresh="Bypass the 24-hour cache and re-call Gemini",
    )
    @app_commands.checks.cooldown(1, 60.0)
    async def analyze(
        interaction: discord.Interaction,
        place_id: str,
        force_refresh: bool = False,
    ):
        await interaction.response.defer(thinking=True)

        try:
            numeric_id = int(place_id)
        except ValueError:
            await interaction.followup.send(
                "❌ Game ID must be a number, e.g. `994732206` for Blox Fruits."
            )
            return

        # Roblox fetch is natively async — call it directly on the loop.
        try:
            info = await get_game_info(str(numeric_id))
        except Exception as exc:
            await interaction.followup.send(
                f"⚠️ Failed to fetch game info: `{exc}`"
            )
            return

        if info is None:
            await interaction.followup.send(
                f"❌ Could not find a Roblox game with Universe ID `{numeric_id}`."
            )
            return

        # Assemble everything Gemini needs in one shape:
        #   • growth %, scout score breakdown, trend score, developer component.
        growth = get_growth(info["id"])
        info["growth"] = growth
        info["scout_score"] = await calculate_scout_score(info)
        info["trend_score"] = calculate_trend_score(info, growth)

        # analyze_game handles cache, quota gate, and fallback internally.
        # Sync Gemini call (requests + SQLite) — run in a thread so the
        # event loop stays responsive while waiting on HTTP.
        analysis = await asyncio.to_thread(
            analyze_game, info, force_refresh=force_refresh
        )
        report = format_report(info["name"], analysis)
        await interaction.followup.send(report)

    @tree.command(
        name="aiusage",
        description="Show today's Gemini quota usage"
    )
    async def aiusage(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        limit = get_daily_limit()
        used = get_usage_today()
        remaining = quota_remaining()

        await interaction.followup.send(
            f"🤖 **AI quota**\n\n"
            f"**Provider:** {provider_status()}\n"
            f"**Daily limit:** {limit}\n"
            f"**Used today:** {used}\n"
            f"**Remaining:** {remaining}\n\n"
            f"_Set `GEMINI_DAILY_LIMIT` in the environment to override the "
            f"default of {limit if limit else '0'}._"
        )

    @tree.command(name="scan", description="Find trending Roblox games")
    @app_commands.checks.cooldown(1, 300.0)
    async def scan(interaction: discord.Interaction):
        """Full discovery scan for the caller's filters.

        Each run costs 100+ Roblox requests, so it is guarded twice: one
        scan at a time process-wide (see _scan_lock) and at most one per
        user per 5 minutes via the cooldown.
        """
        # Defer FIRST to acknowledge the interaction within Discord's 3-second limit
        await interaction.response.defer(thinking=True)

        if _scan_lock.locked():
            await interaction.followup.send(
                "⏳ A scan is already running — it can take a few minutes. "
                "Try again shortly."
            )
            return
        async with _scan_lock:
            user_id = interaction.user.id
            add_user(user_id)
            user_settings = get_user_filters(user_id)

            # Send initial status message
            await interaction.followup.send(
                f"""
🔎 Scanning Roblox Games...

Filters:
CCU: {user_settings['minimum_players']:,} to {user_settings['maximum_players'] or '— (no cap)'}
Visits: below {user_settings['maximum_visits']:,}
Rating: {user_settings['minimum_rating']}%+ required
Requires: Roblox link{' + Discord invite' if user_settings['require_discord'] else ''}{' + on a Roblox chart' if user_settings['require_charts'] else ''}
Growth: {user_settings['minimum_growth']}%
Genre: {user_settings['genre']}
Max Age: {'— (no cap)' if not user_settings['max_age'] else str(user_settings['max_age']) + ' days'}

Please wait while I find the best acquisition opportunities...
"""
            )

            # Use async scanner to avoid blocking the event loop
            async with aiohttp.ClientSession() as session:
                results, diagnostics = await scan_games_async(
                    session=session,
                    user_settings=user_settings,
                    return_diagnostics=True,
                )

            if not results:
                await interaction.followup.send(
                    "No games matched your filters. Try lowering them with `/settings`."
                )
                await interaction.followup.send(
                    "📊 Scan diagnostics\n"
                    f"Total games collected: {diagnostics['total_collected']}\n"
                    f"Games passing CCU filter: {diagnostics['ccu_passes']}\n"
                    f"Games passing visits filter: {diagnostics['visits_passes']}\n"
                    f"Games passing rating filter: {diagnostics['rating_passes']}\n"
                    f"Games passing genre filter: {diagnostics['genre_passes']}\n"
                    f"Games passing age filter: {diagnostics['age_passes']}\n"
                    f"Games with Discord: {diagnostics['discord_present']}\n"
                    f"Games on a Roblox chart: {diagnostics['charts_present']}\n"
                    f"Games with market links: {diagnostics['market_links_present']}\n"
                    f"Final matches: {diagnostics['final_matches']}"
                )
                if diagnostics.get("rejection_note"):
                    await interaction.followup.send(
                        f"⚠️ {diagnostics['rejection_note']}"
                    )
                return

            summary_message = (
                "📊 Scan diagnostics\n"
                f"Total games collected: {diagnostics['total_collected']}\n"
                f"Games passing CCU filter: {diagnostics['ccu_passes']}\n"
                f"Games passing visits filter: {diagnostics['visits_passes']}\n"
                f"Games passing rating filter: {diagnostics['rating_passes']}\n"
                f"Games with Discord: {diagnostics['discord_present']}\n"
                f"Games with market links: {diagnostics['market_links_present']}\n"
                f"Final matches: {diagnostics['final_matches']}"
            )
            if diagnostics.get("rejection_note"):
                summary_message += f"\n⚠️ {diagnostics['rejection_note']}"
            await interaction.followup.send(summary_message)

            # Send the top result as a professional embed with buttons
            top_game = results[0]
            await interaction.followup.send(
                embed=create_game_embed(top_game),
                view=AlertButtons.with_game(top_game)
            )

            # Send remaining results as text summaries
            for index, game in enumerate(results[1:5], start=2):
                await interaction.followup.send(_format_game_message(index, game))

    @tree.command(name="trending", description="Show only high-trend games")
    async def trending(interaction: discord.Interaction):
        # Defer FIRST to acknowledge the interaction within Discord's 3-second limit
        await interaction.response.defer(thinking=True)

        # Send initial status message
        await interaction.followup.send(
            "🔥 Finding trending Roblox games..."
        )

        # Use async scanner to avoid blocking the event loop
        async with aiohttp.ClientSession() as session:
            results = await scan_games_async(session=session)
        
        trending_games = [g for g in results if g.get("trend_score", 0) > 80]

        if not trending_games:
            await interaction.followup.send(
                "No games are trending above 80/100 right now. Try `/scan` for all results."
            )
            return

        message = "🔥 **Trending Roblox Games**\n\n"

        for index, game in enumerate(trending_games[:5], start=1):
            growth = game.get("growth", 0)
            growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"

            message += (
                f"**{index}. {game['name']}**\n\n"
                f"Trend: {game.get('trend_score', 0)}/100\n"
                f"Growth: {growth_text}\n\n"
            )

        await interaction.followup.send(message)

    async def _show_saved_list(interaction: discord.Interaction):
        """Shared implementation for /saved and /watchlist."""
        await interaction.response.defer(thinking=True)

        user_id = interaction.user.id
        saved = get_saved_games_for_user(user_id)

        if not saved:
            await interaction.followup.send(
                "📚 Your saved list is empty. Use **💾 Save** on a scan result to bookmark a game."
            )
            return

        embed = discord.Embed(
            title="📚 Saved Games",
            description="Bookmarks — games you want to remember.",
            color=discord.Color.blue(),
        )

        for game_id, game_name, date_saved in saved:
            embed.add_field(
                name=f"🎮 {game_name}",
                value=f"ID: `{game_id}` | Saved: {date_saved[:10]}",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    @tree.command(name="saved", description="View your saved (bookmarked) games")
    async def saved(interaction: discord.Interaction):
        await _show_saved_list(interaction)

    @tree.command(name="watchlist", description="Alias of /saved (legacy)")
    async def watchlist(interaction: discord.Interaction):
        await _show_saved_list(interaction)

    @tree.command(name="watched", description="View your actively tracked games")
    async def watched(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        user_id = interaction.user.id
        tracked = get_watched_games_for_user(user_id)

        if not tracked:
            await interaction.followup.send(
                "👀 You're not tracking any games yet. Use the **👀 Watch** button on a scan result to monitor a game's growth."
            )
            return

        embed = discord.Embed(
            title="👀 Watched Games",
            description="Tracker — the bot monitors these for major CCU/visits changes.",
            color=discord.Color.orange(),
        )

        for game_id, game_name, last_players, last_visits, date_added in tracked:
            players_text = f"{last_players:,}" if last_players is not None else "—"
            embed.add_field(
                name=f"🎮 {game_name}",
                value=(
                    f"ID: `{game_id}`  |  Last CCU: {players_text}\n"
                    f"Tracking since: {date_added[:10]}"
                ),
                inline=False,
            )

        embed.set_footer(text="Remove with /unwatch <game_id>")
        await interaction.followup.send(embed=embed)

    @tree.command(name="ignored", description="View games you've ignored")
    async def ignored(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        user_id = interaction.user.id
        blocked = get_ignored_games_for_user(user_id)

        if not blocked:
            await interaction.followup.send(
                "🚫 You haven't ignored any games yet."
            )
            return

        embed = discord.Embed(
            title="🚫 Ignored Games",
            description="These games will never appear in alerts again.",
            color=discord.Color.greyple(),
        )

        for game_id, date_ignored in blocked:
            embed.add_field(
                name=f"Game ID: `{game_id}`",
                value=f"Ignored: {date_ignored[:10]}",
                inline=False,
            )

        embed.set_footer(text="Unblock with /unignore <game_id>")
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="unwatch",
        description="Stop tracking a game"
    )
    @app_commands.describe(game_id="Roblox Universe ID to stop watching")
    async def unwatch(interaction: discord.Interaction, game_id: str):
        try:
            numeric_id = int(game_id)
        except ValueError:
            await interaction.response.send_message(
                "❌ Game ID must be a number, e.g. `994732206` for Blox Fruits.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id

        # /unwatch is also the escape hatch from auto-watch: removing your
        # own watch lifts the bot's automatic watch for the same game too,
        # so a game you have vetoed stops being tracked entirely.
        from config import AUTO_WATCH_USER_ID

        auto_owner = int(AUTO_WATCH_USER_ID)
        auto_watch_exists = is_user_watching(auto_owner, numeric_id)

        removed = unwatch_game_for_user(user_id, numeric_id)
        removed_auto = 0
        if auto_owner != user_id and auto_watch_exists:
            removed_auto = unwatch_game_for_user(auto_owner, numeric_id)

        if removed or removed_auto:
            message = f"✅ Removed game `{numeric_id}` from your tracked games."
            if removed_auto and not removed:
                message = (
                    f"✅ The bot was auto-watching game `{numeric_id}` after its "
                    f"alert — that watch is now lifted."
                )
            elif removed_auto:
                message += "\n🤖 The bot's automatic watch for this game was lifted too."
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ You're not tracking game `{numeric_id}`.\n"
                f"Use **/watched** to see what you are tracking.",
                ephemeral=True,
            )

    @tree.command(
        name="unignore",
        description="Allow a previously ignored game to appear in alerts again"
    )
    @app_commands.describe(game_id="Roblox Universe ID to stop ignoring")
    async def unignore(interaction: discord.Interaction, game_id: str):
        try:
            numeric_id = int(game_id)
        except ValueError:
            await interaction.response.send_message(
                "❌ Game ID must be a number.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id
        removed = unignore_game_for_user(user_id, numeric_id)
        if removed:
            await interaction.response.send_message(
                f"✅ Game `{numeric_id}` can appear in alerts again.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ You weren't ignoring game `{numeric_id}`.",
                ephemeral=True,
            )

    @tree.command(
        name="leaderboard",
        description="Top Scout Scores among games the bot has actually alerted",
    )
    @app_commands.describe(
        period="Time window for the ranking",
    )
    @app_commands.choices(period=[
        app_commands.Choice(name="Last 7 days", value="7"),
        app_commands.Choice(name="Last 30 days", value="30"),
        app_commands.Choice(name="All time", value="0"),
    ])
    async def leaderboard(
        interaction: discord.Interaction,
        period: str = "30",
    ):
        """Rank the bot's own alerted games by their peak Scout Score.

        Reads only the alerted_games store — zero Roblox requests, instant.
        Repeat discoveries (alert_count) surface games the scans keep
        converging on, which is itself an acquisition signal.
        """
        from database import top_alerted_by_scout_score

        await interaction.response.defer(thinking=True)
        try:
            days = max(0, int(period))
        except (TypeError, ValueError):
            days = 30

        rows = top_alerted_by_scout_score(limit=10, days=days)
        if not rows:
            await interaction.followup.send(
                "🏁 No alerts recorded yet in this window — the leaderboard "
                "fills up as the bot discovers and alerts on games."
            )
            return

        embed = discord.Embed(
            title=("🏆 Scout Leaderboard — " + ("all time" if days == 0
                   else f"last {days} days")),
            color=discord.Color.gold(),
        )
        lines = []
        for rank, (gid, gname, score, count, first) in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")
            score_text = f"`{score}/100`" if score is not None else "`—`"
            repeat = " · seen " + str(count) if (count or 1) > 1 else ""
            lines.append(f"{medal} {gname or f'ID {gid}'} — {score_text}{repeat}")
        embed.description = "\n".join(lines)
        embed.set_footer(
            text="Peak Scout Score at alert time · /dossier <game_id> for the full read"
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="heatmap",
        description="Weekly discovery-activity grid: alerts per week with average Scout Score",
    )
    @app_commands.describe(period="How far back to render")
    @app_commands.choices(period=[
        app_commands.Choice(name="Last 8 weeks", value="56"),
        app_commands.Choice(name="Last 4 weeks", value="28"),
        app_commands.Choice(name="All time", value="0"),
    ])
    async def heatmap(
        interaction: discord.Interaction,
        period: str = "56",
    ):
        """One bar-line per ISO week: volume + average peak Scout Score.

        A pure local read of alerted_games — first_alerted timestamps plus
        the recorded scores — so it answers instantly and reflects exactly
        what the bot chose to surface over time. Weeks with no alerts are
        compacted away (an empty-string row would just be noise).
        """
        from database import alert_heatmap

        await interaction.response.defer(thinking=True)
        try:
            days = max(0, int(period))
        except (TypeError, ValueError):
            days = 56

        rows = alert_heatmap(days=days)
        if not rows:
            await interaction.followup.send(
                "🗓 No alerts in this window yet — the grid fills as "
                "cycles run and games match.",
            )
            return

        peak = max((c for _, c, _ in rows), default=1) or 1
        lines = []
        for week, count, avg in rows:
            filled = "█" * max(1, round(count / peak * 10))
            score_text = (
                f"avg score {avg:.0f}" if avg is not None else "no scores"
            )
            lines.append(f"{week}: {filled} {count} alert(s), {score_text}")

        embed = discord.Embed(
            title=("🗓 Discovery Heatmap — " + ("all time" if days == 0
                   else f"last {days} days")),
            color=discord.Color.purple(),
            description="```\n" + "\n".join(lines) + "```",
        )
        embed.set_footer(
            text="Volume per ISO week · score = average peak Scout Score at alert time"
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="compare",
        description="Compare two Roblox games side-by-side for acquisition decisions",
    )
    @app_commands.describe(
        game_a="First game's Roblox Universe ID",
        game_b="Second game's Roblox Universe ID",
    )
    async def compare(
        interaction: discord.Interaction,
        game_a: str,
        game_b: str,
    ):
        """Side-by-side comparison of two candidate games.

        Acquisition work is always a comparison, so this renders both games
        on one embed table: CCU, visits, favorites, rating, growth, Scout
        Score with its verdict, and chart presence. One batch request for
        both games (the batch endpoint takes both IDs in a single call),
        fail-soft: a game that cannot be fetched renders as its column of
        dashes, never crashes the command.
        """
        await interaction.response.defer(thinking=True)

        try:
            id_a, id_b = int(game_a.strip()), int(game_b.strip())
        except ValueError:
            await interaction.followup.send(
                "❌ Both game IDs must be numbers, e.g. "
                "`/compare game_a:994732206 game_b:577641401`."
            )
            return

        from roblox_api_async import fetch_game_details_batch, reset_rate_limiters
        from roblox_api_async import enrich_games_with_ratings
        import aiohttp as _aiohttp
        from history import growth_summary as hist_growth
        from velocity import compute_velocity as vel

        fetched = {}
        try:
            # One batch request covers both IDs (the endpoint's limit is 50).
            async with _aiohttp.ClientSession() as session:
                try:
                    results = await fetch_game_details_batch(session, [id_a, id_b])
                finally:
                    reset_rate_limiters()
                for info in results or []:
                    if info and info.get("id"):
                        fetched[info["id"]] = info
                if fetched:
                    await enrich_games_with_ratings(session, list(fetched.values()))
        except Exception as exc:
            await interaction.followup.send(
                f"⚠️ Roblox fetch failed: `{exc}` — try again shortly."
            )
            return

        import chart_rank as _chart_rank

        async def _col(gid: int) -> dict:
            g = fetched.get(gid)
            if g is None:
                return {"name": f"ID {gid}", "missing": True, "id": gid}
            g["growth"] = get_growth(gid)
            try:
                g.update(hist_growth(gid))
            except Exception:
                pass
            try:
                g["velocity"] = vel(gid)
            except Exception:
                g["velocity"] = {}
            try:
                _chart_rank.attach_rank_signal(g)
            except Exception:
                pass
            try:
                g["scout_score"] = await calculate_scout_score(g)
            except Exception:
                pass
            return {"game": g, "missing": False, "id": gid}

        col_a = await _col(id_a)
        col_b = await _col(id_b)

        def _fmt_number(v) -> str:
            try:
                return f"{int(v):,}"
            except (TypeError, ValueError):
                return "—"

        def _fmt_pct(v) -> str:
            if v is None:
                return "—"
            try:
                return f"{float(v):+.1f}%"
            except (TypeError, ValueError):
                return "—"

        def _scout_total(col: dict) -> str:
            if col["missing"]:
                return "—"
            return str(col["game"].get("scout_score", {}).get("total", "—"))

        name_a = col_a["game"].get("name") if not col_a["missing"] else f"ID {id_a}"
        name_b = col_b["game"].get("name") if not col_b["missing"] else f"ID {id_b}"

        def _val(col: dict, key: str):
            if col["missing"]:
                return None
            return col["game"].get(key)

        ccu_a = _val(col_a, "playing") or 0
        ccu_b = _val(col_b, "playing") or 0
        vis_a = _val(col_a, "visits") or 0
        vis_b = _val(col_b, "visits") or 0
        fav_a = _val(col_a, "favorites") or 0
        fav_b = _val(col_b, "favorites") or 0
        rat_a = _val(col_a, "rating_percent")
        rat_b = _val(col_b, "rating_percent")
        gro_a = _val(col_a, "growth")
        gro_b = _val(col_b, "growth")
        cru_a = _val(col_a, "ccu_growth_pct")
        cru_b = _val(col_b, "ccu_growth_pct")
        rank_a = _val(col_a, "chart_rank")
        rank_b = _val(col_b, "chart_rank")

        embed = discord.Embed(
            title="⚖️ Side-by-side comparison",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )

        def _rating_str(v) -> str:
            try:
                return f"{float(v):.1f}%" if v else "—"
            except (TypeError, ValueError):
                return "—"

        def _rank_str(v) -> str:
            try:
                return f"#{int(v)}" if v else "—"
            except (TypeError, ValueError):
                return "—"

        lines = [
            f"{'Metric':<14} {'A — ' + str(name_a)[:28]:<30} {'B — ' + str(name_b)[:28]:<30}",
            f"{'-' * 76}",
            f"{'CCU':<14} {_fmt_number(ccu_a):>28} {_fmt_number(ccu_b):>28}",
            f"{'Visits':<14} {_fmt_number(vis_a):>28} {_fmt_number(vis_b):>28}",
            f"{'Favorites':<14} {_fmt_number(fav_a):>28} {_fmt_number(fav_b):>28}",
            f"{'Rating':<14} {_rating_str(rat_a):>28} {_rating_str(rat_b):>28}",
            f"{'Growth (2-snap)':<14} {_fmt_pct(gro_a):>28} {_fmt_pct(gro_b):>28}",
            f"{'CCU (window)':<14} {_fmt_pct(cru_a):>28} {_fmt_pct(cru_b):>28}",
            f"{'Chart rank':<14} {_rank_str(rank_a):>28} {_rank_str(rank_b):>28}",
            f"{'Scout Score':<14} {_scout_total(col_a):>28} {_scout_total(col_b):>28}",
        ]

        verdict_a = (
            col_a["game"].get("scout_score", {}).get("verdict")
            if not col_a["missing"] else None
        )
        verdict_b = (
            col_b["game"].get("scout_score", {}).get("verdict")
            if not col_b["missing"] else None
        )
        if verdict_a or verdict_b:
            lines.append("")
            lines.append(
                f"Verdicts — A: {verdict_a or '—'} · B: {verdict_b or '—'}"
            )
        if col_a["missing"] or col_b["missing"]:
            lines.append("")
            missing_ids = [
                gid for col, gid in ((col_a, id_a), (col_b, id_b)) if col["missing"]
            ]
            missing_ids_block = ", ".join(str(g) for g in missing_ids)
            lines.append(
                f"Could not fetch: {missing_ids_block} — check the IDs "
                f"(/game <id> works for one game)."
            )

        embed.description = "```\n" + "\n".join(lines) + "```"
        await interaction.followup.send(embed=embed)
