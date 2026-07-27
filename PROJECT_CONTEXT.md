# Roblox Game Scout Bot - Project Context

## Purpose
Roblox Game Scout Bot is a Discord bot that finds Roblox games with growth potential and alerts users about opportunities.

## Technology Stack
- Python
- discord.py
- SQLite database
- Roblox APIs
- RoTrends data
- Gemini AI
- Discord slash commands

## Current Features

### Discovery System
- Scans Roblox games
- Uses RoTrends and Roblox data
- Calculates Scout Score
- Finds trending opportunities

### Discord Features
- Slash commands
- Game scanning
- Game information lookup
- Watchlist system
- Alerts channel
- AI Scout channel

### Tracking System
- Scheduler runs automatically
- Tracks watched games
- Detects CCU/visit growth
- Sends Discord alerts

### AI System
- Gemini AI game analysis
- /analyze command
- AI Scout conversation channel
- Cached AI responses

## Important Files

main.py
- Bot startup
- Discord connection
- Event handling

commands.py
- Slash commands

scheduler.py
- Background tasks

tracker.py
- Watches games and sends alerts

scanner.py
- Scout Score calculations

database.py
- SQLite database operations

gemini_analyzer.py
- AI analysis

rotrends.py
- RoTrends integration

roblox_api.py
- Roblox API requests

## Development Rules

Before changing code:
1. Understand existing systems.
2. Do not remove working features.
3. Explain major changes before making them.
4. Test after modifications.
5. Keep compatibility with discord.py.

## Current Goal

Turn Roblox Game Scout Bot into a reliable 24/7 Roblox game discovery and analysis platform.