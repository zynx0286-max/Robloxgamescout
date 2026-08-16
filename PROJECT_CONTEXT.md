# Roblox Game Scout Bot - Project Context

## Purpose
Roblox Game Scout Bot is a Discord bot that finds Roblox games with growth potential and alerts users about opportunities.

## Technology Stack
- Python
- discord.py
- SQLite database
- Roblox APIs
- Roblox Charts (official explore API), RoMonitor Stats, Creator Exchange
- Gemini AI
- Discord slash commands
- FastAPI (public portfolio live-data API)

## Current Features

### Discovery System
- Scans Roblox games
- Uses Roblox Charts explore API and Roblox data
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

### Portfolio Live-Data System
- Worker polls official Roblox APIs (games + votes + thumbnails) on an interval
- Writes snapshots to dedicated tables (portfolio_games, game_snapshots, discord_snapshots)
- FastAPI serves public endpoints for Framer portfolio cards with short-TTL caching
- Growth computed from historical snapshots (1h / 6h / 24h / 7d)
- Manage games via manage_portfolio.py CLI; never mix personal info with raw Roblox data

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

trending_sources.py
- Roblox Charts (official explore API) + RoMonitor / Creator Exchange links

roblox_api.py
- Roblox API requests

portfolio_worker.py
- Portfolio snapshot collector (Roblox Games / votes / thumbnails APIs)

portfolio_api.py
- FastAPI public portfolio endpoints (cached)

portfolio_db.py
- Portfolio tables + snapshot/growth storage

manage_portfolio.py
- CLI to add/update/hide/remove portfolio games

## Development Rules

Before changing code:
1. Understand existing systems.
2. Do not remove working features.
3. Explain major changes before making them.
4. Test after modifications.
5. Keep compatibility with discord.py.

## Current Goal

Turn Roblox Game Scout Bot into a reliable 24/7 Roblox game discovery and analysis platform.