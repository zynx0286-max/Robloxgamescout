import discord
from discord.ext import commands
from config import TOKEN, PREFIX
from database import init_db

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    await init_db()

# Load commands cog
async def load_extensions():
    await bot.load_extension("commands")

import asyncio

async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
