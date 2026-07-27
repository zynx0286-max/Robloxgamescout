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
    """Reply to messages posted in the configured #ai-scout channel."""
    # bot.user not set during pre-ready; defer to handler which early-exits.
    if bot.user is not None and message.author == bot.user:
        return
    reply = await handle_ai_scout_message(message)
    if not reply:
        return
    async with message.channel.typing():
        await message.channel.send(reply)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    start_scheduler(bot)

async def _shutdown():
    stop_scheduler()
    await bot.close()

bot.run(TOKEN)
