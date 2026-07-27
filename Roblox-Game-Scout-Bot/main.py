import discord
from discord import app_commands
import os
from dotenv import load_dotenv
from database import create_database
from commands import setup_commands
from scheduler import start_scheduler, stop_scheduler
from ai_scout_channel import handle_ai_scout_message

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Required so on_message can read message.content in the #ai-scout
        # private channel. The handler is also gated by channel id.
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        create_database()
        setup_commands(self.tree)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))

            # Sync commands to your test server
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to test server: {GUILD_ID}")

            # Clear old global commands so they don't show as duplicates
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            print("Old global commands cleared")
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
        await message.channel.send(reply)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    start_scheduler(bot)

async def _shutdown():
    stop_scheduler()
    await bot.close()

bot.run(TOKEN)
