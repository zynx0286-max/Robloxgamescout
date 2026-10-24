# Config samples env vars once at import

`config.py` reads every setting at **import time** (`AI_PROVIDER = os.getenv(...)`,
etc.). Anything that mutates `os.environ` later — tests, runtime config changes,
a dashboard writing env vars — does not propagate to code that imported the
constant.

Consequence seen live: `_active_provider()` read the module-level `AI_PROVIDER`,
so a test that set `AI_PROVIDER=gemini` after import still got `auto` behavior
and the suite failed with `'groq' != 'gemini'`.

Rule: for settings that tests or operators may change at runtime, re-read
`os.environ` at call time (falling back to the config constant), e.g.
`os.environ.get("AI_PROVIDER") or AI_PROVIDER or "auto"`. Read-only, stable
infrastructure settings can stay import-time.
