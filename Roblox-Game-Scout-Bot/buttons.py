"""
Alert buttons for the Game Scout.

Every alert embed includes these interaction buttons:
  ✅ Claim    — bookmark the opportunity (saved list)
  ✅ Accept   — accept the recommendation (bookmark + track)
  ❌ Reject   — reject the opportunity (never shown again)
  ℹ️ More Info — show deeper AI analysis

Persistence across restarts
---------------------------
The buttons are :class:`discord.ui.DynamicItem` subclasses: each button's
``custom_id`` carries the game id (``alert_claim:12345``) and the class
``template`` regex matches that id even when the view object that sent the
message is long gone. discord.py dispatches a matching interaction through
the registered template (``bot.add_dynamic_items(...)`` at startup), so
button presses keep working on alerts posted before any restart. The
previous design registered one persistent view with bare ``alert_claim``
ids, which discord.py matches only exactly — every pre-restart press died
with "This interaction failed".
"""

import asyncio
import logging
from typing import Optional

import discord

from database import save_game_for_user, ignore_game_for_user, watch_game_for_user

logger = logging.getLogger("buttons")


async def _fetch_game(game_id: int) -> Optional[dict]:
    """Fetch a game dict by universe id, off the event loop.

    Used when a press arrives on an alert the current process never sent
    (post-restart), where no in-memory game dict exists.
    """
    from roblox_api import get_game_info_sync

    info = await asyncio.to_thread(get_game_info_sync, str(game_id))
    if info is not None:
        info.setdefault("id", game_id)
        info.setdefault("name", info.get("name", f"Game {game_id}"))
    return info


class _AlertButton(discord.ui.DynamicItem[discord.ui.Button], template=r"(?!)"):
    """Shared plumbing for one alert action.

    Subclasses provide ``template`` (regex over the custom id), button
    styling, and an ``_apply`` coroutine that performs the action. ``game``
    is present when the press lands in the same session that sent the
    alert; post-restart presses resolve the game from Roblox by id.

    The base class itself carries a never-matching template (``(?!)``)
    because discord.py requires one on every DynamicItem subclass; only
    the four concrete actions are ever registered.
    """

    def __init__(self, label: str, style: discord.ButtonStyle, game_id: int,
                 game: Optional[dict] = None):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"{self.ACTION}:{game_id}",
            )
        )
        self.game_id = game_id
        self.game = game

    ACTION = ""

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        game = self.game or await _fetch_game(self.game_id)
        if game is None:
            await interaction.followup.send(
                "⚠️ Could not identify the game for this action.",
                ephemeral=True,
            )
            return
        try:
            reply = await self._apply(interaction, game)
        except Exception as exc:
            logger.exception("Alert button %s failed for game %s", self.ACTION, self.game_id)
            reply = f"⚠️ That action failed: `{exc}`"
        await interaction.followup.send(reply, ephemeral=True)

    async def _apply(self, interaction: discord.Interaction, game: dict) -> str:
        raise NotImplementedError


class ClaimButton(_AlertButton, template=r"alert_claim:(?P<game_id>\d+)"):
    """✅ Claim — bookmark the opportunity in the user's saved list."""

    ACTION = "alert_claim"

    def __init__(self, game_id: int, game: Optional[dict] = None):
        super().__init__("✅ Claim", discord.ButtonStyle.green, game_id, game)

    async def _apply(self, interaction: discord.Interaction, game: dict) -> str:
        save_game_for_user(interaction.user.id, game)
        return (
            f"✅ **Claimed**: {game['name']}\n"
            f"Saved to your list. Use ✅ **Accept** to also track it, "
            f"or `/watch {game['id']}` to start tracking now."
        )


class AcceptButton(_AlertButton, template=r"alert_accept:(?P<game_id>\d+)"):
    """✅ Accept — accept the recommendation: bookmark AND track the game."""

    ACTION = "alert_accept"

    def __init__(self, game_id: int, game: Optional[dict] = None):
        super().__init__("✅ Accept", discord.ButtonStyle.green, game_id, game)

    async def _apply(self, interaction: discord.Interaction, game: dict) -> str:
        save_game_for_user(interaction.user.id, game)
        watch_game_for_user(interaction.user.id, game)
        return (
            f"✅ **Accepted**: {game['name']}\n"
            f"Added to your watchlist — the tracker will report growth "
            f"and chart moves on this game."
        )


class RejectButton(_AlertButton, template=r"alert_reject:(?P<game_id>\d+)"):
    """❌ Reject — never show this game to this user again."""

    ACTION = "alert_reject"

    def __init__(self, game_id: int, game: Optional[dict] = None):
        super().__init__("❌ Reject", discord.ButtonStyle.red, game_id, game)

    async def _apply(self, interaction: discord.Interaction, game: dict) -> str:
        ignore_game_for_user(interaction.user.id, game["id"])
        return (
            f"❌ **Rejected**: {game['name']}\n"
            f"This opportunity has been rejected and won't be shown again."
        )


class MoreInfoButton(_AlertButton, template=r"alert_more_info:(?P<game_id>\d+)"):
    """ℹ️ More Info — deeper AI analysis (cached 24h, quota-gated)."""

    ACTION = "alert_more_info"

    def __init__(self, game_id: int, game: Optional[dict] = None):
        super().__init__("ℹ️ More Info", discord.ButtonStyle.blurple, game_id, game)

    async def _apply(self, interaction: discord.Interaction, game: dict) -> str:
        from gemini_analyzer import analyze_game

        # Sync requests+SQLite — run in a worker thread so the event loop
        # stays responsive (the AI call can retry for tens of seconds).
        analysis = await asyncio.to_thread(analyze_game, game, force_refresh=False)
        verdict = analysis.get("verdict", "Unknown")
        confidence = analysis.get("confidence", 0)
        strengths = analysis.get("strengths") or []
        risks = analysis.get("risks") or []
        recommendation = analysis.get("recommendation") or "—"

        strengths_text = "\n".join(f"✅ {s}" for s in strengths) or "—"
        risks_text = "\n".join(f"⚠️ {r}" for r in risks) or "—"

        return (
            f"🤖 **AI Analyst Report**\n\n"
            f"**Game:** {game.get('name', 'Unknown')}\n\n"
            f"**Verdict:** {verdict}\n\n"
            f"**Confidence:** {confidence}%\n\n"
            f"**Why this matters:**\n{strengths_text}\n\n"
            f"**Risks:**\n{risks_text}\n\n"
            f"**Recommendation:** {recommendation}"
        )


ALL_ALERT_BUTTONS = (ClaimButton, AcceptButton, RejectButton, MoreInfoButton)


class AlertButtons(discord.ui.View):
    """The four alert actions for one game, as a sendable view.

    Each child is a :class:`DynamicItem` whose custom id carries the game
    id, so presses dispatch through the registered templates in every
    session — including sessions after a restart. Nothing here relies on
    the view instance staying alive.
    """

    def __init__(self, game: Optional[dict] = None):
        super().__init__(timeout=None)
        game_id = int(game["id"]) if game else 0
        for button_cls in ALL_ALERT_BUTTONS:
            self.add_item(button_cls(game_id, game))

    @classmethod
    def with_game(cls, game: dict) -> "AlertButtons":
        """Factory used by embed builders: the view bound to one game."""
        return cls(game=game)
