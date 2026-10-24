@echo off
REM One-click local run for Windows: creates the venv on first run, then starts the bot.
cd /d "%~dp0"
if not exist .venv (
    echo First run - creating virtual environment...
    py -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
    echo.
    echo No .env found - copying .env.example. Edit .env and add your DISCORD_TOKEN!
    copy .env.example .env >nul
    notepad .env
)
echo Starting Roblox Game Scout bot...
.venv\Scripts\python main.py
pause
