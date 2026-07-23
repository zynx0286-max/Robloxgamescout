import discord
from discord import app_commands
from database import add_user, get_user, update_user
from roblox_api import get_game_info
from scanner import calculate_score, scan_games


def _reasons(game):
    """Build a list of positive signals for a game."""
    reasons = []
    if game.get("growth", 0) >= 50:
        reasons.append("✅ Fast player growth")
    if game["playing"] >= 1000:
        reasons.append("✅ High activity")
    if game["visits"] >= 1000000:
        reasons.append("✅ Strong engagement")
    if game["favorites"] >= 10000:
        reasons.append("✅ Popular with players")
    if not reasons:
        reasons.append("ℹ️ Low activity right now")
    return "\n".join(reasons)


def setup_commands(tree: app_commands.CommandTree):
    """Register all slash commands with the bot's command tree."""

    @tree.command(name="hello", description="Check if the bot is working")
    async def hello(interaction: discord.Interaction):
        await interaction.response.send_message(
            "Roblox Game Scout Bot is online!"
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
{data[1]:,}

Minimum Players:
{data[2]}

Genre:
{data[3]}
"""
        )

    @tree.command(name="settings", description="Update your scout filters")
    @app_commands.describe(
        visits="Minimum number of visits a game must have",
        players="Minimum number of active players",
        genre="Genre to filter by (e.g. Simulator, RPG, Any)",
    )
    async def settings(
        interaction: discord.Interaction,
        visits: int = None,
        players: int = None,
        genre: str = None,
    ):
        user_id = interaction.user.id

        add_user(user_id)
        update_user(user_id, visits, players, genre)

        data = get_user(user_id)

        await interaction.response.send_message(
            f"""
⚙️ Settings Updated

Minimum Visits:
{data[1]:,}

Minimum Players:
{data[2]}

Genre:
{data[3]}
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
        await interaction.response.send_message(
            "🔎 Scanning Roblox games..."
        )

        results = scan_games()

        if not results:
            await interaction.followup.send(
                "No games found."
            )
            return

        for game in results[:5]:
            growth = game.get("growth", 0)
            growth_text = f"+{growth}%" if growth > 0 else f"{growth}%"

            message = (
                f"🎮 **{game['name']}**\n\n"
                f"👥 Players:\n{game['playing']:,}\n\n"
                f"📈 Growth:\n{growth_text}\n\n"
                f"⭐ Score:\n{game['score']}/100\n\n"
                f"📝 Why:\n{_reasons(game)}"
            )

            await interaction.followup.send(message)
