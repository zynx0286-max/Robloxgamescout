# Roblox Game Scout

Roblox Game Scout is an acquisition-focused Discord bot that discovers Roblox games with strong commercial potential by combining live Roblox data, Roblox Charts signals, Discord/social link presence, and lightweight scoring.

## What it does

- Scans Roblox games for acquisition-style opportunities
- Enforces acquisition-friendly defaults such as CCU, visits, rating, Discord presence, and market-link availability (Roblox Charts / RoMonitor / Creator Exchange)
- Runs slash commands in Discord such as /scan, /settings, /profile, and /analyze
- Supports automated scanning and alerting workflows
- Serves live portfolio stats to Framer via a FastAPI endpoint (portfolio_worker.py + portfolio_api.py)

## Features

- Discord slash commands
- Filtered game discovery pipeline
- Async scanning for faster operation
- Diagnostics summary for scan results
- Optional AI-assisted analysis integration

## Project structure

- Roblox-Game-Scout-Bot/ — Discord bot implementation
- artifacts/ — generated artifacts and samples
- lib/ — shared libraries and supporting packages

## Getting started

1. Install Python dependencies
   ```bash
   cd Roblox-Game-Scout-Bot
   python -m pip install -r requirements.txt
   ```
2. Configure environment variables for your bot token and database path
3. Start the bot
   ```bash
   python main.py
   ```

## Legal / compliance note

This project is intended for research, analysis, and bot automation workflows. Users are responsible for complying with Roblox, Discord, and third-party platform terms of service, API usage policies, and applicable laws.

## Disclaimer

This repository is provided for informational and development purposes. It does not guarantee the accuracy of market signals or investment or acquisition outcomes.
