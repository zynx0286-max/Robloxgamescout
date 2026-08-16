FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/scoutbot.db

WORKDIR /app

COPY Roblox-Game-Scout-Bot/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY Roblox-Game-Scout-Bot/ /app/

EXPOSE 8000

# Default: run collector + API together (shared SQLite, see start.sh).
# Override the command to run just one half:
#   docker run <image> python portfolio_worker.py --once
CMD ["sh", "start.sh"]
