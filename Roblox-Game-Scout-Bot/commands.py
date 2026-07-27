import discord
from discord import app_commands
from database import (
    add_user,
    get_user_filters,
    get_user_row,
    update_user,
    get_saved_games_for_user,
    get_watched_games_for_user,
    get_ignored_games_for_user,
    unwatch_game_for_user,
    unignore_game_for_user,
    is_user_watching,
)
from roblox_api import get_game_info
from scanner import calculate_score, scan_games
from categories import get_category
from embeds import create_game_embed
from buttons import GameButtons, AlertButtons


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
        await interaction.response.send_message(
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
            view=AlertButtons(sample_game),
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

    @tree.command(name="about", description="Learn what this bot does")
    async def about(interaction: discord.Interaction):
        await interaction.response.send_message(
            "🎮 **Roblox Game Scout Bot**\n\n"
            "A tool that finds Roblox games with:\n"
            "• High growth potential\n"
            "• Trending activity\n"
            "• Strong player numbers\n"
            "• Developer opportunities"
        )

    @tree.command(name="profile", description="View your scout settings")
    async def profile(interaction: discord.Interaction):
        user_id = interaction.user.id

        add_user(user_id)
        row = get_user_row(user_id)

        if row is None:
            await interaction.response.send_message(
                "⚙️ No profile yet — set filters with **/settings**."
            )
            return

        f = {
            "min_visits": int(row[1] or 0),
            "max_visits": int(row[2] or 0),
            "min_players": int(row[3] or 0),
            "max_players": int(row[4] or 0),
            "min_growth": int(row[5] or 0),
            "genre": row[6] or "Any",
            "max_age": int(row[7] or 0),
        }

        def _v(x):
            return "— (no cap)" if x == 0 else f"{x:,}"

        await interaction.response.send_message(
            f"""
🎮 Scout Profile

Minimum Visits:
{f['min_visits']:,}

Maximum Visits:
{_v(f['max_visits'])}

Minimum Players:
{f['min_players']:,}

Maximum Players:
{_v(f['max_players'])}

Minimum Growth:
{f['min_growth']}%

Genre Filter:
{f['genre']}

Max Game Age:
{f['max_age']} days
"""
        )

    @tree.command(
        name="settings",
        description="Change your scout filters (any field can be omitted)",
    )
    @app_commands.describe(
        min_visits="Minimum number of visits a game must have",
        max_visits="Maximum visits (0 = no cap)",
        min_players="Minimum number of active players",
        max_players="Maximum players (0 = no cap)",
        min_growth="Minimum growth percentage (e.g. 30)",
        genre="Genre keyword to filter by (Simulator, RPG, Tycoon, Any…)",
        max_age="Maximum age in days (only enforced if timestamp is known)",
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
    ):
        user_id = interaction.user.id

        add_user(user_id)
        update_user(
            user_id,
            minimum_visits=min_visits,
            maximum_visits=max_visits,
            minimum_players=min_players,
            maximum_players=max_players,
            minimum_growth=min_growth,
            genre=genre,
            max_age=max_age,
        )

        f = get_user_filters(user_id)

        def _v(x):
            return "— (no cap)" if x == 0 else f"{x:,}"

        await interaction.response.send_message(
            f"""
⚙️ Scout Settings Updated

Minimum Visits: {f['minimum_visits']:,}
Maximum Visits: {_v(f['maximum_visits'])}

Minimum Players: {f['minimum_players']:,}
Maximum Players: {_v(f['maximum_players'])}

Minimum Growth: {f['minimum_growth']}%
Genre Filter: {f['genre']}
Max Game Age: {f['max_age']} days
""".strip()
        )

    @tree.command(name="game", description="Get Roblox game information")
    @app_commands.describe(place_id="Roblox game Universe ID")
    async def game(interaction: discord.Interaction, place_id: str):
        data = get_game_info(place_id)

        if data is None:
            await interaction.response.send_message(
                "Could not find that Roblox game."
            )
            return

        await interaction.response.send_message(
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

    @tree.command(name="scan", description="Find trending Roblox games")
    async def scan(interaction: discord.Interaction):
        user_id = interaction.user.id
        add_user(user_id)
        user_settings = get_user_filters(user_id)

        await interaction.response.send_message(
            f"""
🔎 Scan Complete

Filters:
Players: {user_settings['minimum_players']:,}+ (max {user_settings['maximum_players']:,})
Visits: {user_settings['minimum_visits']:,}+ (max {user_settings['maximum_visits']:,})
Growth: {user_settings['minimum_growth']}%
Genre: {user_settings['genre']}
Max Age: {user_settings['max_age']} days

Scanning...
"""
        )

        results = scan_games(user_settings)

        if not results:
            await interaction.followup.send(
                "No games matched your filters. Try lowering them with `/settings`."
            )
            return

        # Send the top result as a professional embed with buttons
        top_game = results[0]
        await interaction.followup.send(
            embed=create_game_embed(top_game),
            view=GameButtons(top_game)
        )

        # Send remaining results as text summaries
        for index, game in enumerate(results[1:5], start=2):
            await interaction.followup.send(_format_game_message(index, game))

    @tree.command(name="trending", description="Show only high-trend games")
    async def trending(interaction: discord.Interaction):
        await interaction.response.send_message(
            "🔥 Finding trending Roblox games..."
        )

        results = scan_games()
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
        user_id = interaction.user.id
        saved = get_saved_games_for_user(user_id)

        if not saved:
            await interaction.response.send_message(
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

        await interaction.response.send_message(embed=embed)

    @tree.command(name="saved", description="View your saved (bookmarked) games")
    async def saved(interaction: discord.Interaction):
        await _show_saved_list(interaction)

    @tree.command(name="watchlist", description="Alias of /saved (legacy)")
    async def watchlist(interaction: discord.Interaction):
        await _show_saved_list(interaction)

    @tree.command(name="watched", description="View your actively tracked games")
    async def watched(interaction: discord.Interaction):
        user_id = interaction.user.id
        tracked = get_watched_games_for_user(user_id)

        if not tracked:
            await interaction.response.send_message(
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
        await interaction.response.send_message(embed=embed)

    @tree.command(name="ignored", description="View games you've ignored")
    async def ignored(interaction: discord.Interaction):
        user_id = interaction.user.id
        blocked = get_ignored_games_for_user(user_id)

        if not blocked:
            await interaction.response.send_message(
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
        await interaction.response.send_message(embed=embed)

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

        if not is_user_watching(user_id, numeric_id):
            await interaction.response.send_message(
                f"❌ You're not tracking game `{numeric_id}`.\n"
                f"Use **/watched** to see what you are tracking.",
                ephemeral=True,
            )
            return

        removed = unwatch_game_for_user(user_id, numeric_id)
        if removed:
            await interaction.response.send_message(
                f"✅ Removed game `{numeric_id}` from your tracked games.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Could not remove game `{numeric_id}` — try again.",
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
