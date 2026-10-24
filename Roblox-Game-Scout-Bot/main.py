import discord
from discord import app_commands
import os
import re
from dotenv import load_dotenv
from database import create_database
from commands import setup_commands
from scheduler import start_scheduler
from ai_scout_channel import handle_ai_scout_message
from buttons import ALL_ALERT_BUTTONS
from config import BOT_VERSION
import health_check

load_dotenv()

# A bot token is three dot-separated segments of [A-Za-z0-9_-]. Used only to
# warn before login when the value cannot possibly be one (see
# _check_startup_env); Discord itself remains the final judge.
_BOT_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
)


def _clean_env_str(name: str, strip_bot_prefix: bool = False):
    """Read an env var with the noise panel input fields leave on it.

    Returns ``(value, cleaned)``. Whitespace and quote characters around a
    value are the most common reason an otherwise-correct variable — a bot
    token above all — gets rejected downstream ("Improper token has been
    passed"). A token pasted from Discord's own HTTP examples can also carry
    a ``Bot `` prefix; that is stripped too when ``strip_bot_prefix`` is set
    (no real token or ID contains spaces, so removing it is always safe).
    """
    raw = os.getenv(name)
    if raw is None:
        return None, False
    value = raw.strip()
    if strip_bot_prefix and value.lower().startswith("bot "):
        value = value[4:]
    value = value.strip().strip('"').strip("'").strip()
    return value, value != raw


TOKEN, TOKEN_NEEDED_CLEANING = _clean_env_str("DISCORD_TOKEN", strip_bot_prefix=True)
GUILD_ID, GUILD_ID_NEEDED_CLEANING = _clean_env_str("GUILD_ID")

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Required so on_message can read message.content in the #ai-scout
        # private channel. The handler is also gated by channel id.
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # Route every uncaught slash-command error through one handler.
        # Without this a crashing command just leaves the user hanging on
        # "did not respond" with nothing in the console to diagnose from.
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        """Log the full traceback AND tell the user something went wrong.

        Fail-soft: if even the error message can't be delivered (expired
        webhook, permissions), we swallow it — the console traceback is
        the source of truth.
        """
        import traceback

        # Cooldown rejections are routine, not errors: answer the user with
        # the wait time instead of a traceback and a generic apology.
        if isinstance(error, app_commands.CommandOnCooldown):
            try:
                await interaction.response.send_message(
                    f"⏳ `/{interaction.command.qualified_name}` is on cooldown — "
                    f"try again in {error.retry_after:.0f}s.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        original = getattr(error, "original", error)
        print(
            f"⚠️ Error in /{getattr(interaction.command, 'qualified_name', '?')} "
            f"from user {interaction.user}:"
        )
        traceback.print_exception(
            type(original), original, original.__traceback__
        )

        user_text = (
            "⚠️ Something went wrong running that command. "
            "The error was logged in the bot console."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(user_text, ephemeral=True)
            else:
                await interaction.response.send_message(
                    user_text, ephemeral=True
                )
        except Exception:
            # Followup webhook expired or send failed — console already
            # has the real traceback, so nothing more to do.
            pass

    async def setup_hook(self):
        create_database()
        setup_commands(self.tree)

        # Probe every external dependency once at boot so a dead endpoint
        # (e.g. Roblox retiring an API) is caught in the logs immediately,
        # not weeks later from empty alerts. Fail-soft: never blocks startup.
        try:
            await health_check.run_startup_health_check()
        except Exception as exc:
            print(f"Startup health check failed to run: {exc}")

        # Register the alert buttons as DynamicItems so Discord interaction
        # dispatches reach the bot even after a restart: each button's
        # custom_id carries the game id ("alert_claim:12345") and is matched
        # by its class template regex. A plain persistent view cannot do
        # this — discord.py matches persistent views by exact custom_id, so
        # every press on a pre-restart alert used to fail silently.
        self.add_dynamic_items(*ALL_ALERT_BUTTONS)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))

            # Guild-scoped sync updates instantly, so it's safe every startup.
            # clear_commands + copy_global_to keeps the guild set in lockstep
            # with the code (removes commands deleted from commands.py too).
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to test server: {GUILD_ID}")

            # NOTE: do NOT clear/sync global commands here. Discord caches
            # global commands for up to 1 hour, so re-syncing them on every
            # startup causes ~1h of command downtime after each restart.
            # Global command set only changes when GUILD_ID is removed.
            print(
                "Global commands left untouched (they cache for up to 1h on "
                "Discord's side; re-syncing every restart causes downtime)."
            )
        else:
            await self.tree.sync()
            print("Slash commands synced globally")

bot = MyBot()


@bot.event
async def on_message(message):
    """Reply to messages posted in the configured #ai-scout channel.

    Discord's ``async with channel.typing()`` context manager fires the
    typing indicator the instant it is entered, then refreshes it every
    ~5 s until the block exits. So we run the *cheap* filters locally
    before opening the context, then commit to replying and do all
    downstream work (Roblox fetch, history, Gemini) inside it. This way
    the channel shows ``"Roblox Game Scout is typing…"`` immediately on
    any user message — exactly the same UX members see when a regular
    user is composing a reply.
    """
    if bot.user is not None and message.author == bot.user:
        return
    if message.author.bot:
        return

    from ai_scout_channel import resolve_ai_channel_id
    if message.channel.id != resolve_ai_channel_id():
        return

    # Fire the typing indicator *before* any heavy I/O so the channel sees
    # "Bot is typing…" right away, just like a regular Discord user.
    async with message.channel.typing():
        reply = await handle_ai_scout_message(
            message, bot.user.id if bot.user else 0
        )
        if not reply:
            return
        # Ping the requester so they get a Discord notification, just like
        # being @-mentioned by any other channel member.
        ping = f"<@{message.author.id}>\n"
        await message.channel.send(ping + reply)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    start_scheduler(bot)

def _print_startup_banner() -> None:
    """Print the resolved configuration once, at boot.

    Wispbyte's panel can silently drop environment variables (e.g. when it
    updates a template), and a Pterodactyl console scrolled back is painful
    to read. Dumping the schedule + which vars arrived makes every future
    "why isn't it scanning?" question answerable from the log alone.
    """
    import sys
    from config import (
        ALERT_CHANNEL_ID,
        AUTO_WATCH_ALERTS,
        AUTO_WATCH_MAX,
        DATABASE_PATH,
        DIGEST_ENABLED,
        DIGEST_HOUR_ET,
        SCAN_ACTIVE_HOURS,
        SCAN_END_HOUR_ET,
        SCAN_INTERVAL,
        SCAN_START_HOUR_ET,
    )

    if SCAN_ACTIVE_HOURS and SCAN_END_HOUR_ET > SCAN_START_HOUR_ET:
        schedule = (
            f"every {SCAN_INTERVAL}m, "
            f"{SCAN_START_HOUR_ET}:00-{SCAN_END_HOUR_ET}:00 ET"
        )
    else:
        schedule = f"every {SCAN_INTERVAL}m, 24/7 (no time window)"
    digest = f"{DIGEST_HOUR_ET}:00 ET daily" if DIGEST_ENABLED else "disabled"

    print("-" * 68)
    print(f"Roblox Game Scout v{BOT_VERSION}")
    print(f"  Python        : {sys.version.split()[0]}")
    print(f"  Working dir   : {os.getcwd()}")
    print(f"  Guild         : {GUILD_ID or 'NOT SET (global sync)'}")
    print(f"  Alert channel : {ALERT_CHANNEL_ID or 'NOT SET (alerts skipped)'}")
    print(f"  Scan schedule : {schedule}")
    print(f"  Morning digest: {digest}")
    auto_watch = (
        f"on (cap {AUTO_WATCH_MAX})" if AUTO_WATCH_ALERTS else "off"
    )
    print(f"  Auto-watch    : {auto_watch}")
    print(f"  Database      : {DATABASE_PATH}")
    print("-" * 68)


def _check_startup_env() -> None:
    """Fail fast with an actionable message instead of a discord.py crash.

    A missing token otherwise surfaces deep inside discord.py as
    "TypeError: expected token to be a str, received NoneType instead",
    which says nothing about the actual cause. Print what the bot can and
    cannot see, then exit non-zero.
    """
    if not TOKEN:
        print("=" * 68)
        print("FATAL: DISCORD_TOKEN is not set — the bot cannot log in.")
        print("")
        print("This is a host configuration problem, not a code problem.")
        print("On your host panel (Wispbyte / Pterodactyl):")
        print("  1. Open the server -> Startup tab")
        print("  2. Add this environment variable:")
        print("         DISCORD_TOKEN  =  your bot token")
        print("  3. Recommended, so the bot is fully functional:")
        print("         GUILD_ID         =  your server ID")
        print("         ALERT_CHANNEL_ID =  the channel for automatic alerts")
        print("  4. Press Save, then Start the server")
        print("")
        print("What the bot can currently see:")
        print("  DISCORD_TOKEN    : MISSING")
        print(f"  GUILD_ID         : {GUILD_ID or 'not set'}")
        print(f"  ALERT_CHANNEL_ID : {os.getenv('ALERT_CHANNEL_ID') or 'not set'}")
        print("=" * 68)
        raise SystemExit(1)

    if TOKEN_NEEDED_CLEANING:
        print(
            "NOTE: DISCORD_TOKEN had stray whitespace/quotes around it — "
            "cleaned automatically before login."
        )
    if not _BOT_TOKEN_RE.fullmatch(TOKEN):
        print(
            "WARNING: DISCORD_TOKEN does not look like a bot token (a bot "
            "token is three dot-separated segments). Usual mix-ups: the "
            "Client Secret or Application ID pasted instead of the Bot "
            "Token (Developer Portal -> your app -> Bot -> Reset Token), "
            "or only part of the token copied. Trying to log in anyway."
        )
    if GUILD_ID_NEEDED_CLEANING:
        print(
            "NOTE: GUILD_ID had stray whitespace/quotes — cleaned "
            "automatically."
        )

    if not GUILD_ID:
        print(
            "WARNING: GUILD_ID is not set — commands will sync globally, "
            "which can take up to 1 hour to appear in Discord."
        )
    if not os.getenv("ALERT_CHANNEL_ID"):
        print(
            "WARNING: ALERT_CHANNEL_ID is not set — scheduled scan alerts "
            "will be skipped. Use /scan-now to test manually."
        )


# Import-safety guard: importing this module (e.g. for tests or tooling)
# must not attempt a Discord login. `bot.run` needs a valid token and would
# otherwise raise at import time.
if __name__ == "__main__":
    _check_startup_env()
    _print_startup_banner()
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        # Discord answered, so the token WAS delivered — it just is not a
        # valid bot token. Say so in plain language instead of leaving a raw
        # discord.py traceback on a host console, then exit non-zero so the
        # host still records the crash.
        print("=" * 68)
        print("FATAL: Discord rejected the bot token (LoginFailure).")
        print("")
        print("Discord was reached, so the value WAS delivered — it just is")
        print("not a valid bot token. Usual causes, in order:")
        print("  1. The Client Secret or Application ID was pasted instead of")
        print("     the Bot Token. The Bot Token lives at: Developer Portal ->")
        print("     your application -> Bot -> Reset Token. It is shown ONCE;")
        print("     copy the whole value (three dot-separated segments,")
        print("     70+ characters).")
        print("  2. The token was reset after it was copied: pressing Reset")
        print("     Token invalidates every older copy immediately.")
        print("  3. Only part of the token was copied.")
        print("  4. It is a personal account token, not a bot token — this")
        print("     bot needs the Bot Token of an application you own.")
        print("")
        print("Fix DISCORD_TOKEN in the host panel (Startup / Variables tab)")
        print("or in the .env next to main.py, then restart. No quotes or")
        print("spaces around the value.")
        print("=" * 68)
        raise SystemExit(1)
