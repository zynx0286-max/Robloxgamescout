import discord
from discord import app_commands
import os
from dotenv import load_dotenv
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
        setup_commands(self.tree)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            # Clear old guild commands first to avoid duplicates
            self.tree.clear_commands(guild=guild)
            # Copy global commands to guild and sync
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to test server: {GUILD_ID}")
        else:
            await self.tree.sync()
            print("Slash commands synced globally")

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)
