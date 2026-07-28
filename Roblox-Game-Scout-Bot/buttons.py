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
            f"💾 **{self.game['name']}** bookmarked. View it later with `/watchlist`.",
            ephemeral=True,
        )


class AlertButtons(discord.ui.View):
    """Persistent buttons that appear on automated alert embeds.

    💾 Save  → bookmark only (saved_games, viewable via /watchlist)
    👀 Watch → active tracking (watched_games, scheduler will monitor deltas)
    ❌ Ignore → suppress future alerts (ignored_games)

    This view is registered once at startup so Discord interaction dispatches
    reach the bot even after restarts. Game data is looked up dynamically from
    the interaction, so the view can be created without constructor arguments.
    """

    def __init__(self, game=None):
        super().__init__(timeout=None)
        # ``game`` is stored for per-alert rendering helpers when the view
        # is created alongside an embed.  Persistent registration only needs
        # a no-args instantiation, so it is optional.
        if game is not None:
            self.game = game

    def _get_game(self, interaction: discord.Interaction):
        """Resolve the game dict from the interaction using the custom_id.

        Each button stores the ``game_id`` in its ``custom_id`` so the view
        can recover the full record even when the original ``game`` dict is
        unavailable (persistent registration / restarts). The game info is
        fetched from Roblox on demand.
        """
        game_id = None
        custom_id = interaction.data.get("custom_id", "")
        # Expected custom_ids: alert_save:{game_id}, alert_watch:{game_id}, alert_ignore:{game_id}
        if ":" in custom_id:
            _, game_id_str = custom_id.rsplit(":", 1)
            try:
                game_id = int(game_id_str)
            except ValueError:
                game_id = None

        if game_id is None:
            return None

        # Import here to avoid circular imports and keep Roblox access lazy
        from roblox_api import get_game_info
        info = get_game_info(str(game_id))
        if info is not None:
            # Ensure required fields are present for downstream button handlers.
            info.setdefault("id", game_id)
            info.setdefault("name", info.get("name", f"Game {game_id}"))
        return info

    @discord.ui.button(
        label="💾 Save",
        style=discord.ButtonStyle.green,
        custom_id="alert_save",
    )
    async def save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        game = getattr(self, "game", None) or self._get_game(interaction)
        if game is None:
            await interaction.followup.send(
                "⚠️ Could not identify the game for this action.",
                ephemeral=True,
            )
            return
        save_game_for_user(interaction.user.id, game)
        await interaction.followup.send(
            f"💾 **Saved**: {game['name']}\n"
            f"Bookmark only — view later with `/watchlist`.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="👀 Watch",
        style=discord.ButtonStyle.blurple,
        custom_id="alert_watch",
    )
    async def watch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        game = getattr(self, "game", None) or self._get_game(interaction)
        if game is None:
            await interaction.followup.send(
                "⚠️ Could not identify the game for this action.",
                ephemeral=True,
            )
            return
        watch_game_for_user(interaction.user.id, game)
        await interaction.followup.send(
            f"👀 **Now tracking**: {game['name']}\n"
            f"You'll get a 🚀 alert if its player count changes significantly.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="❌ Ignore",
        style=discord.ButtonStyle.red,
        custom_id="alert_ignore",
    )
    async def ignore(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        game = getattr(self, "game", None) or self._get_game(interaction)
        if game is None:
            await interaction.followup.send(
                "⚠️ Could not identify the game for this action.",
                ephemeral=True,
            )
            return
        ignore_game_for_user(interaction.user.id, game["id"])
        await interaction.followup.send(
            f"❌ **{game['name']}** ignored. It won't be posted again.",
            ephemeral=True,
        )

    @classmethod
    def with_game(cls, game):
        """Factory helper for embeds: returns a view with game-specific custom_ids."""
        view = cls(game=game)
        for child in view.children:
            if hasattr(child, "custom_id") and child.custom_id in {"alert_save", "alert_watch", "alert_ignore"}:
                child.custom_id = f"{child.custom_id}:{game['id']}"
        return view