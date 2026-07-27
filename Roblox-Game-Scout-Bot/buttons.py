import discord
from database import save_game_for_user, ignore_game_for_user, watch_game_for_user


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
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        save_game_for_user(user_id, self.game)
        await interaction.followup.send(
            f"💾 **{self.game['name']}** saved to your watchlist!",
            ephemeral=True,
        )


class AlertButtons(discord.ui.View):
    """Buttons that appear on automated alert embeds."""

    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(
        label="💾 Save",
        style=discord.ButtonStyle.green,
    )
    async def save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        save_game_for_user(interaction.user.id, self.game)
        await interaction.followup.send(
            f"💾 **{self.game['name']}** saved to your watchlist!",
            ephemeral=True,
        )

    @discord.ui.button(
        label="👀 Watch",
        style=discord.ButtonStyle.blurple,
    )
    async def watch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        watch_game_for_user(interaction.user.id, self.game)
        await interaction.followup.send(
            f"👀 **{self.game['name']}** added to your watch list.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="❌ Ignore",
        style=discord.ButtonStyle.red,
    )
    async def ignore(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        ignore_game_for_user(interaction.user.id, self.game["id"])
        await interaction.followup.send(
            f"❌ **{self.game['name']}** ignored. It won't be posted again.",
            ephemeral=True,
        )
