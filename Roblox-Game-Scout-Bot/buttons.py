"""
Alert buttons for the Game Scout.

Every alert embed includes these interaction buttons:
  ✅ Claim  — claim this acquisition opportunity
  ✅ Accept — accept the acquisition recommendation
  ❌ Reject — reject the opportunity
  ℹ️ More Info — show deeper AI analysis
"""

import discord
from database import save_game_for_user, ignore_game_for_user, watch_game_for_user


class AlertButtons(discord.ui.View):
    """Persistent buttons that appear on automated alert embeds.

    ✅ Claim  → Claim this acquisition opportunity
    ✅ Accept → Accept the acquisition recommendation
    ❌ Reject → Reject the opportunity
    ℹ️ More Info → Show deeper AI analysis

    This view is registered once at startup so Discord interaction dispatches
    reach the bot even after restarts. Game data is looked up dynamically from
    the interaction.
    """

    def __init__(self, game=None):
        super().__init__(timeout=None)
        if game is not None:
            self.game = game

    def _get_game(self, interaction: discord.Interaction):
        """Resolve the game dict from the interaction using the custom_id."""
        game_id = None
        custom_id = interaction.data.get("custom_id", "")
        if ":" in custom_id:
            _, game_id_str = custom_id.rsplit(":", 1)
            try:
                game_id = int(game_id_str)
            except ValueError:
                game_id = None

        if game_id is None:
            return None

        from roblox_api import get_game_info
        info = get_game_info(str(game_id))
        if info is not None:
            info.setdefault("id", game_id)
            info.setdefault("name", info.get("name", f"Game {game_id}"))
        return info

    @discord.ui.button(
        label="✅ Claim",
        style=discord.ButtonStyle.green,
        custom_id="alert_claim",
    )
    async def claim(
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
        watch_game_for_user(interaction.user.id, game)
        await interaction.followup.send(
            f"✅ **Claimed**: {game['name']}\n"
            f"This acquisition opportunity has been claimed and added to your watchlist.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="✅ Accept",
        style=discord.ButtonStyle.green,
        custom_id="alert_accept",
    )
    async def accept(
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
        watch_game_for_user(interaction.user.id, game)
        await interaction.followup.send(
            f"✅ **Accepted**: {game['name']}\n"
            f"Acquisition recommendation accepted. The game has been added to your watchlist.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="❌ Reject",
        style=discord.ButtonStyle.red,
        custom_id="alert_reject",
    )
    async def reject(
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
            f"❌ **Rejected**: {game['name']}\n"
            f"This opportunity has been rejected and won't be shown again.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="ℹ️ More Info",
        style=discord.ButtonStyle.blurple,
        custom_id="alert_more_info",
    )
    async def more_info(
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

        # Fetch AI analysis
        from gemini_analyzer import analyze_game
        analysis = analyze_game(game, force_refresh=False)
        verdict = analysis.get("verdict", "Unknown")
        confidence = analysis.get("confidence", 0)
        strengths = analysis.get("strengths") or []
        risks = analysis.get("risks") or []
        recommendation = analysis.get("recommendation") or "—"

        strengths_text = "\n".join(f"✅ {s}" for s in strengths) or "—"
        risks_text = "\n".join(f"⚠️ {r}" for r in risks) or "—"

        report = (
            f"🤖 **AI Analyst Report**\n\n"
            f"**Game:** {game.get('name', 'Unknown')}\n\n"
            f"**Verdict:** {verdict}\n\n"
            f"**Confidence:** {confidence}%\n\n"
            f"**Why this matters:**\n{strengths_text}\n\n"
            f"**Risks:**\n{risks_text}\n\n"
            f"**Recommendation:** {recommendation}"
        )

        await interaction.followup.send(report, ephemeral=True)

    @classmethod
    def with_game(cls, game):
        """Factory helper for embeds: returns a view with game-specific custom_ids."""
        view = cls(game=game)
        for child in view.children:
            if hasattr(child, "custom_id") and child.custom_id in {
                "alert_claim", "alert_accept", "alert_reject", "alert_more_info"
            }:
                child.custom_id = f"{child.custom_id}:{game['id']}"
        return view