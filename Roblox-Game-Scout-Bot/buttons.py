import discord
from database import save_game_for_user


class GameButtons(discord.ui.View):
    """Discord buttons for a single game result."""

    def __init__(self, game):
        super().__init__()
        self.game = game

    @discord.ui.button(
        label="💾 Save Game",
        style=discord.ButtonStyle.green
    )
    async def save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        user_id = interaction.user.id
        save_game_for_user(user_id, self.game)
        await interaction.response.send_message(
            f"💾 **{self.game['name']}** saved to your watchlist!",
            ephemeral=True
        )
