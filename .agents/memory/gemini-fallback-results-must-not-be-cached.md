# Gemini fallback and quota results must not enter the 24h cache

`analyze_game()` caches per game for 24h. The first version cached **every**
result, including the `_fallback()` (Gemini unavailable) and
`_quota_blocked_result()` (daily quota exhausted) verdicts.

Consequence: one outage or one busy day would pin "AI analysis not enabled"
onto a game's cache row for 24h. Even after fixing the real cause (e.g. a bad
`GEMINI_MODEL` name), every re-analysis of that game kept serving the stale
fallback until the TTL expired.

Rule (fixed 2026-09-19): write to `ai_analysis_cache` only after a
**successful** live Gemini call. Failure/quota paths return the normalized
fallback directly without `_cache_put()`, so the next attempt retries for
real.

Related trap: module-level diagnostic state (`_LAST_GEMINI_ERROR`) must be
declared `global` inside the function that assigns it. Without `global`, the
assignment creates a local and `_fallback()` reads a stale value — the 404
misdiagnosis from `gemini-model-404-misdiagnosis.md` came back as
`"... failed: None"`. Regression tests live in
`tests/test_gemini_analyzer.py::test_fallback_result_is_not_cached` and
`test_model_404_reports_model_problem_not_missing_key`.

Scheduler note: run `passes_alert_filters()` **before** `analyze_game()` in
the scan pipeline. Filtering after AI analysis burned the whole daily Gemini
quota on games that were about to be rejected by the hard filters anyway.
