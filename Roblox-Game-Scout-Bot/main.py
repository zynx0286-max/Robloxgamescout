import discord
from discord import app_commands
import os
from dotenv import load_dotenv
from database import create_database
from commands import setup_commands

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
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
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)
