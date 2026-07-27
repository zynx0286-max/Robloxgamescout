---
name: Gemini model 404 masquerading as "key not configured"
description: When a pinned Gemini model returns 404, a generic `_fallback()` builds a "key not configured" message even though the key is fine — which misleads users into re-adding a working secret.
---

If `analyze_game()` (or any other Gemini wrapper) falls through to a `_fallback()` whose placeholder strings include phrases like *"Gemini key not configured"* or *"Set GEMINI_API_KEY in environment to enable"*, those exact strings get stored in the response's `strengths`/`risks`/`recommendation` and re-emitted on every subsequent call (cached or not). So a user seeing *"AI analysis not enabled. Set GEMINI_API_KEY..."* in the report concludes their key is broken — when the actual cause is a **retired Gemini model name returning 404**.

**Why:** diamond-shaped failure mode. The Gemini API returns 404 (model retired for that API version), `_call_gemini()` catches it and returns `None`, `analyze_game()` then takes the `_fallback()` path, and that fallback string literal happens to mention the secret the user just added. The diagnostic message is *correct* for one failure mode (no key) and *wrong* for another (bad model name) — so the user spends time re-checking secrets instead of fixing model retirement.

**How to apply:**
- In `gemini_analyzer.py`-style wrappers, **always prefer the `gemini-flash-latest` alias** over pinned names like `gemini-1.5-flash` / `gemini-2.5-flash`. Pinned names are deprecated on a rolling basis for new keys; the `*-latest` aliases route to whatever is currently supported on the user's tier.
- The placeholder text in any `_fallback()` should **distinguish** "no key" from "call failed for another reason", or omit the secret-related copy entirely when the failure isn't a missing key.
- On "AI not enabled" complaints, the diagnostic order is: (1) `pgrep -af python.*main.py` for duplicates and check `cat /proc/<pid>/environ` for the actual key, (2) cache delete + restart, (3) `v1beta/models?key=<K>` ListModels to verify the model still exists, (4) probe `v1beta/models/<m>:generateContent?key=<K>` to confirm the path works. Don't ask the user to re-add the key until step 3+4 have exonerated the pinned model.
