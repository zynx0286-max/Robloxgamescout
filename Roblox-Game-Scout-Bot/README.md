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

## Portfolio live-data (free hosting)

The portfolio stack (`portfolio_worker.py` + `portfolio_api.py`) is designed to run on a free tier. Pick one:

### Recommended: Render (free, GitHub-native)

1. Push this repo to GitHub (already done).
2. In the Render dashboard choose **New > Blueprint**, connect the repo, and confirm the `render.yaml` blueprint.
3. Render builds the Dockerfile and runs one free web service that starts both the snapshot collector and the API via `start.sh`.
4. Your API URL is `<service>.onrender.com` — point Framer at `/api/v1/portfolio`.

Notes on free tier:
- A single service is used on purpose: free instances get their own disk, so a separate worker would write a database the API can't see. `start.sh` keeps both halves in one container.
- Free web services spin down after ~15 min without traffic. Hitting `/api/v1/health` from Framer (or a cron) every few minutes keeps it warm.
- Alternatively run the collector as a cron job (`python portfolio_worker.py --once`) and skip the long-lived worker.

### Docker (any host)

```bash
docker build -t game-scout .
docker run -p 8000:8000 game-scout                    # collector + API
docker run game-scout python portfolio_worker.py --once   # one-off snapshot
```

### Manual (any always-on VM/container)

```bash
# terminal 1 — collector
python portfolio_worker.py --interval 60
# terminal 2 — API
python -m uvicorn portfolio_api:app --host 0.0.0.0 --port 8000
```

## Legal / compliance note

This project is intended for research, analysis, and bot automation workflows. Users are responsible for complying with Roblox, Discord, and third-party platform terms of service, API usage policies, and applicable laws.

## Disclaimer

This repository is provided for informational and development purposes. It does not guarantee the accuracy of market signals or investment or acquisition outcomes.
