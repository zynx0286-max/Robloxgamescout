import discord
from discord import app_commands
from database import add_user, get_user, update_user, get_saved_games_for_user
from roblox_api import get_game_info
from scanner import calculate_score, scan_games
from categories import get_category
from embeds import create_game_embed
from buttons import GameButtons


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
        data = get_user(user_id)

        await interaction.response.send_message(
            f"""
🎮 Scout Profile

Minimum Visits:
{int(data[1]):,}

Minimum Players:
{int(data[2]):,}

Minimum Growth:
{int(data[3])}%

Genre:
{data[4]}

Max Age:
{int(data[5])} days
"""
        )

    @tree.command(name="settings", description="Change your scout filters")
    @app_commands.describe(
        visits="Minimum number of visits a game must have",
        players="Minimum number of active players",
        growth="Minimum growth percentage (e.g. 50)",
        genre="Genre to filter by (e.g. Simulator, RPG, Any)",
        max_age="Maximum game age in days",
    )
    async def settings(
        interaction: discord.Interaction,
        visits: int = None,
        players: int = None,
        growth: int = None,
        genre: str = None,
        max_age: int = None,
    ):
        user_id = interaction.user.id

        add_user(user_id)
        update_user(user_id, visits, players, growth, genre, max_age)

        data = get_user(user_id)

        await interaction.response.send_message(
            f"""
⚙️ Scout Settings Updated

Minimum Visits:
{int(data[1]):,}

Minimum Players:
{int(data[2]):,}

Minimum Growth:
{int(data[3])}%

Genre:
{data[4]}

Max Age:
{int(data[5])} days
"""
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
        data = get_user(user_id)

        user_settings = {
            "minimum_visits": int(data[1]) if data[1] is not None else 0,
            "minimum_players": int(data[2]) if data[2] is not None else 0,
            "minimum_growth": int(data[3]) if data[3] is not None else 0,
        }

        await interaction.response.send_message(
            f"""
🔎 Scan Complete

Filters:
Players: {user_settings['minimum_players']:,}+
Visits: {user_settings['minimum_visits']:,}+
Growth: {user_settings['minimum_growth']}%

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

    @tree.command(name="watchlist", description="View your saved games")
    async def watchlist(interaction: discord.Interaction):
        user_id = interaction.user.id
        saved = get_saved_games_for_user(user_id)

        if not saved:
            await interaction.response.send_message(
                "Your watchlist is empty. Use the 💾 Save Game button on a scan result."
            )
            return

        embed = discord.Embed(
            title="💾 Saved Games",
            color=discord.Color.blue(),
        )

        for game_id, game_name, date_saved in saved:
            embed.add_field(
                name=game_name,
                value=f"ID: `{game_id}` | Saved: {date_saved[:10]}",
                inline=False
            )

        await interaction.response.send_message(embed=embed)
