"""The slash-command error handler answers cooldowns like a human.

Cooldown rejections are routine, not errors: the handler must reply with
the wait time in a friendly ephemeral message instead of the generic
"something went wrong" apology a traceback path produces.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord
from discord import app_commands


import main as main_module


def _cooldown(retry_after):
    """Build the exception exactly as discord.py raises it on a cooldown."""
    return app_commands.CommandOnCooldown(
        cooldown=app_commands.Cooldown(rate=1, per=300.0),
        retry_after=retry_after,
    )


def _fake_interaction(qualified_name: str, done: bool = False):
    sent = []

    async def send_message(content=None, ephemeral=False):
        sent.append((content, ephemeral))

    return types.SimpleNamespace(
        command=types.SimpleNamespace(qualified_name=qualified_name),
        user=types.SimpleNamespace(id=1),
        response=types.SimpleNamespace(send_message=send_message, is_done=lambda: done),
        _sent=sent,
    )


def test_cooldown_rejection_gets_a_friendly_message():
    bot = main_module.MyBot()
    interaction = _fake_interaction("scan")
    err = _cooldown(42.0)

    asyncio.run(bot.on_app_command_error(interaction, err))

    assert len(interaction._sent) == 1
    content, ephemeral = interaction._sent[0]
    assert "/scan" in content and "42" in content
    assert ephemeral is True


def test_cooldown_path_never_touches_the_traceback_path():
    """A cooldown must return before the generic error plumbing runs."""
    bot = main_module.MyBot()
    interaction = _fake_interaction("analyze")

    # The generic path would attempt followup/ephemeral apology and print a
    # traceback; with a cooldown it must simply answer and return.
    err = _cooldown(5.0)
    asyncio.run(bot.on_app_command_error(interaction, err))

    assert len(interaction._sent) == 1  # exactly one reply, nothing else
