import discord
from discord import app_commands
import os
from dotenv import load_dotenv
from commands import setup_commands

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # Optional: your Discord server ID for fast testing

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        setup_commands(self.tree)

        if GUILD_ID:
            # Sync to one server instantly (great for testing)
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to test server: {GUILD_ID}")
        else:
            # Global sync (can take up to 1 hour to appear)
            await self.tree.sync()
            print("Slash commands synced globally (may take up to 1 hour)")

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)
