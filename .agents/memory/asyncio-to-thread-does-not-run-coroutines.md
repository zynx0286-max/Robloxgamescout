# Awaiting async functions through asyncio.to_thread

`asyncio.to_thread(fn, ...)` runs a **sync callable** in a worker thread.
Passing it an `async def` function does not run it — it merely *creates a
coroutine object* inside the thread and returns it. The body never executes.

Symptoms seen in this repo (fixed 2026-09-19):

- `tracker.py`: `await asyncio.to_thread(get_game_info, game_id)` raised
  `RuntimeError: coroutine ... was never awaited` for **every** watched game —
  the watchlist tracker alerted on nothing.
- `ai_scout_channel.py` (analyze path): same pattern, so `@bot <game_id>`
  analyze requests crashed instead of posting a report.
- `commands.py` `/game` and `/analyze`: `to_thread(get_game_info, ...)`
  returned a coroutine object into `data`/`info`; every downstream field
  access failed and the interaction timed out.

Rule: before wrapping any call in `to_thread`, check whether the callee is
`async def`. If it is, `await` it directly on the event loop instead. Reserve
`to_thread` for genuinely sync/blocking calls (the `requests`-based
`get_game_info_sync`, `analyze_game`, `answer_question`).

Smoke test that catches this class-wide:

    python -c "import importlib,pathlib; [importlib.import_module(f.stem) for f in pathlib.Path('.').glob('*.py') if f.name != 'main.py']"

(Syntax errors like `await` outside async land at import time; un-awaited
coroutines need a runtime test of the call site.)
