---
name: Discord.py stacked command decorators
description: Why two `@tree.command(...)` decorators on the same async function fail, and the right pattern for aliased slash commands.
---

Stacking `@tree.command(name="watchlist", ...)` above `@tree.command(name="saved", ...)` above a single `async def watchlist(...)` raises `TypeError: command function must be a coroutine function` at boot.

**Why:** the inner decorator wraps the coroutine into a registered handler object. The outer decorator then sees a non-async wrapper and rejects it. discord.py 2.x only treats a top-level `async def` as a command callback.

**How to apply:** when you want two slash-command names to share an implementation, split into two async functions that both call a small helper:

```python
async def _show_saved_list(interaction): ...

@tree.command(name="saved", description="...")
async def saved(interaction):
    await _show_saved_list(interaction)

@tree.command(name="watchlist", description="Alias of /saved")
async def watchlist(interaction):
    await _show_saved_list(interaction)
```

Stacking works for plain `@functools.wraps` but **not** for `@tree.command` because that decorator swaps the wrapped value for its own command object.
