# Roblox retired /v1/games/list and /v1/games/recommendations

Verified live 2026-09: both legacy discovery endpoints now return **404**
for unauthenticated callers:

- `GET games.roblox.com/v1/games/list?model.keyword=...` → 404 (also with
  POST JSON body)
- `GET games.roblox.com/v1/games/recommendations?model.universeIds=...` → 404
- `apis.roblox.com/search/games/details` → 404 too

`roblox_search.py` previously built the whole keyword/new/updated discovery
on top of these, so every keyword search silently returned zero games and
scans quietly degraded to curated seeds + Charts.

## What works now

- **Keyword search**: `GET apis.roblox.com/search-api/omni-search
  ?searchQuery={kw}&pageType=all&sessionId={uuid}`. The `sessionId` is
  **mandatory** — without it the API returns HTTP 200 with *empty* results,
  which looks like "no games found" rather than an error. Each Game hit
  carries universeId, playerCount, rootPlaceId, votes, creatorName.
- **Charts discovery** (unchanged): `apis.roblox.com/explore-api/v1/
  get-sort-content?sessionId={uuid}&sortId={Top_Trending_V6|Up_And_Coming_V6|
  CCU_Based_V1|...}` — named sort IDs work; bare numeric gameSetTargetIds
  from get-sorts return "Sort not found or not enabled".
- The batch details API (`games.roblox.com/v1/games?universeIds=...`) is
  unaffected.

Rule of thumb: when a discovery source returns "0 games" repeatedly, probe
the raw endpoint with curl before assuming the keyword list is the problem.
