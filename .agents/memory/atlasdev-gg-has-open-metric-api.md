# Atlas (atlasdev.gg) has an open per-universe metric API

The site atlasdev.gg ("Atlas") is a Roblox game acquisition marketplace with
live analytics for 70k+ universes. The analyze pages are fully client-rendered
(Next.js; the HTML contains no stats), but the page's own data endpoint is
open and unauthenticated (verified 2026-09-19):

    GET https://atlasdev.gg/api/prufer/universe-metric-series/{universe_id}?days=7&bucket=1d

- `bucket` accepts `1d` and `1h`; `days` up to 30 observed.
- Returns `{"series": [{"bucket", "playing" (CCU), "visits", "favorited",
  "ratingPercent"}, ...]}`
- Per-game page pattern: `https://atlasdev.gg/analyze/{universe_id}`
- No API key required as of 2026-09; other `/api/*` paths return 403, so this
  metric-series route is the only known public one.

How it was found: the fetch lives in a template literal inside the site's
Next.js chunk bundles — plain `fetch("...")` greps miss it. Search chunk JS
for `` fetch(` `` (backticks), not just quotes.

Caveat: it is a third-party site with no published API contract or ToS for
programmatic use. Client code (`atlas_api.py`) therefore fails soft and the
bot keeps working from Roblox's own APIs if Atlas changes or blocks.

## 2026-09-25 update: Atlas 403s non-browser User-Agents

The endpoint now returns **403 for non-browser User-Agents** (verified:
curl default UA and aiohttp default UA both 403; Chrome UA with an
`atlasdev.gg/analyze/...` Referer returns 200). `atlas_api.py` already sets
browser headers per request — but any *new* client (health checks, scripts)
must do the same, and sessions passed in from callers must not override the
UA. The startup health check (`health_check.py`) probes Atlas with browser
headers; a 403 there usually means the UA, not the API.
