---
name: Sync HTTP inside discord.py async coroutine
description: `requests.get(...)` inside an `async def scheduled_scan` callsite blocks the entire gateway event loop, starving all slash-command acks.
---

When a slash command times out with **"this application did not respond"** even though the bot is logged in, has open sockets, and the command handler is `defer()`-guarded, the root cause is usually **sync I/O inside an async coroutine**. A `requests.get(url, timeout=10)` call runs the urllib code on the asyncio thread; until it returns (or times out), nothing else dispatched through that loop runs — including the ack of an incoming InteractionCreate event.

**Why:** discord.py, APScheduler's `@tasks.loop`-style loops, and any `await asgiref.sync`-free codepath run coroutines on a **single** asyncio loop. `requests` is `def` (sync). When called from a coroutine body, the entire asyncio reactor stalls for the call's wall-clock duration. With Roblox topology calls timing out at 10 s, the bot can lose ~10 s per call, and even `/hello` (which has its own handler chain) gets starved during that window.

**How to apply:**
- In `scheduler.py`-style periodic async loops that fan out to `requests.get`, **wrap the sync call** in `asyncio.to_thread(func, *args)`. This moves the blocking call to a worker thread and frees the loop. Example:
  ```python
  games = await asyncio.to_thread(collect_games)  # instead of collect_games()
  ```
- For paths that fan out 30+ sync HTTP calls per scan, run them inside **one** `to_thread` block (or `loop.run_in_executor`) rather than per-call, to amortize thread-pool churn.
- The cheap smoke test: if a fresh `python -u main.py` boots, prints all four log lines, and connects — but `/hello` timeouts within 60 s — there is a sync call somewhere. Set `SCAN_INTERVAL` so high that the loop never runs (`SCAN_INTERVAL=10000`) and re-test; if it now works, the diagnosis is confirmed.
- The interaction-timeout symptom looks identical to the duplicate-process race ([discord-duplicate-bot-processes.md](discord-duplicate-bot-processes.md)) — always verify `pgrep -af 'python.*main\.py'` is a single PID first, then if that is clean, suspect sync I/O.
