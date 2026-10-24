FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/scoutbot.db

WORKDIR /app

COPY Roblox-Game-Scout-Bot/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY Roblox-Game-Scout-Bot/ /app/
# start.sh lives at the repo root, not inside the bot directory. Without this
# copy the container exits immediately with "can't open start.sh".
COPY start.sh ./start.sh
RUN chmod +x ./start.sh

EXPOSE 8000

# Liveness for orchestrators (Docker, Render): the portfolio API answering
# /api/v1/health means uvicorn is up. PORT may be remapped by the host, so
# read it here just like start.sh does.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/api/v1/health', timeout=8).status==200 else 1)"

# Default: run collector + API together (shared SQLite, see start.sh).
# Override the command to run just one half:
#   docker run <image> python portfolio_worker.py --once
CMD ["sh", "start.sh"]
