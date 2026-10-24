"""
Startup dependency health check.

Probes every external service the bot depends on and logs one line per
dependency with its status. Catching a dead endpoint at boot (like the
retired /v1/games/list that silently zeroed discovery for months) beats
discovering it from "why are alerts empty?" weeks later.

All probes are best-effort and fail soft — a dependency being down never
blocks startup.
"""

import asyncio
import logging
import os
import uuid

import aiohttp

from config import ENABLE_ROBLOX_CHARTS

logger = logging.getLogger("health_check")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Filled by run_startup_health_check(); consumed by the /status command.
LAST_RESULTS = []  # list of (name, status_str, detail)


def _ok(name, detail=""):
    return (name, "✅", detail)


def _warn(name, detail=""):
    return (name, "⚠️", detail)


def _fail(name, detail=""):
    return (name, "❌", detail)


# --------------------------------------------------------------------------
# Payload validators — the "reachable but useless" class of failure.
#
# Several Roblox endpoints answer HTTP 200 with a body that contains no
# usable data (omni-search without a sessionId, the explore API when a sort
# is retired, the batch endpoint when the response shape changes). A probe
# that only checks the status code calls those healthy, and discovery dies
# silently. Each validator is a pure (data) -> (ok, detail) function so it
# can be unit-tested without any network.
# --------------------------------------------------------------------------

def validate_games_payload(data) -> tuple[bool, str]:
    """games.roblox.com/v1/games batch payload must carry id + name."""
    games = (data or {}).get("data") or []
    if not games:
        return False, "200 but no game data returned"
    first = games[0] or {}
    if not first.get("id") or not first.get("name"):
        return False, "200 but entries are missing id/name (API shape changed)"
    return True, str(first.get("name", ""))


def validate_omni_search_payload(data) -> tuple[bool, str]:
    """omni-search answers 200-but-empty without a valid sessionId."""
    results = (data or {}).get("searchResults") or []
    hits = sum(len(g.get("contents") or []) for g in results)
    if hits:
        return True, f"{hits} results"
    return False, "reachable but returned 0 results (API may have changed)"


def validate_charts_payload(data) -> tuple[bool, str]:
    """The explore API can answer 200 with an empty games list."""
    games = (data or {}).get("games") or []
    if not games:
        return False, "reachable but returned no games (API may have changed)"
    first = games[0] or {}
    if not (first.get("universeId") or first.get("id")):
        return False, "200 but entries are missing universeId (API shape changed)"
    return True, f"{len(games)} games on sort"


def _ai_status():
    """Status of the AI analyst without any network call."""
    import gemini_analyzer

    provider = gemini_analyzer._active_provider()
    if provider is None:
        return _warn(
            "AI analyst",
            "no provider key set — AI analysis disabled. Add GROQ_API_KEY "
            "(free, console.groq.com) or GEMINI_API_KEY as an environment "
            "variable, then restart, to turn AI scoring on.",
        )
    return _ok("AI analyst", gemini_analyzer.provider_status())


async def run_startup_health_check():
    """Probe all external dependencies. Returns the results list.

    Always creates its own session with browser-like headers: Roblox's
    public web APIs 403 requests that identify as a bare script client, so
    probes must match what the real clients send.
    """
    global LAST_RESULTS

    session = aiohttp.ClientSession(headers=_HEADERS, timeout=_TIMEOUT)

    results = []

    # 1. Roblox batch details API (core enrichment + tracker).
    #    games.roblox.com throttles cloud/datacenter IPs much harder than
    #    residential ones, so a bare 429 is common on a shared free host
    #    and says nothing about the API being broken. Retry a few times
    #    (the real client does the same) and only call it a failure if the
    #    throttle persists.
    async def get_games_status():
        url = "https://games.roblox.com/v1/games?universeIds=994732206"
        name = "Roblox games API"
        for attempt in range(4):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        payload_ok, detail = validate_games_payload(data)
                        if payload_ok:
                            return _ok(name, detail)
                        return _warn(name, detail)
                    if resp.status == 429:
                        if attempt >= 3:
                            return _warn(
                                name,
                                "rate limited (HTTP 429) after 4 tries — this host's "
                                "IP is throttled by Roblox. Raise "
                                "ROBLOX_API_MIN_INTERVAL to 1.0 in the panel's "
                                "environment variables, then restart.",
                            )
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            wait = min(float(retry_after), 5.0) if retry_after else 0.0
                        except (TypeError, ValueError):
                            wait = 0.0
                        await asyncio.sleep(max(wait, 1.0 * (attempt + 1)))
                        continue
                    return _fail(name, f"HTTP {resp.status}")
            except Exception as exc:
                return _fail(name, repr(exc))
        return _warn(name, "rate limited (HTTP 429)")

    results.append(await get_games_status())

    # 2. omni-search (keyword discovery) — returns 200-but-empty without
    #    a sessionId, so assert we actually get results back.
    try:
        url = (
            "https://apis.roblox.com/search-api/omni-search"
            f"?searchQuery=simulator&pageType=all&sessionId={uuid.uuid4()}"
        )
        async with session.get(url) as resp:
            if resp.status != 200:
                results.append(_fail("Roblox omni-search", f"HTTP {resp.status}"))
            else:
                data = await resp.json(content_type=None)
                payload_ok, detail = validate_omni_search_payload(data)
                if payload_ok:
                    results.append(_ok("Roblox omni-search", detail))
                else:
                    results.append(_warn("Roblox omni-search", detail))
    except Exception as exc:
        results.append(_fail("Roblox omni-search", repr(exc)))

    # 3. Roblox Charts explore API (trending discovery) — a bare status
    #    check used to pass here even when the sort came back empty.
    if ENABLE_ROBLOX_CHARTS:
        name = "Roblox Charts"
        url = (
            "https://apis.roblox.com/explore-api/v1/get-sort-content"
            "?sessionId=" + str(uuid.uuid4()) + "&sortId=Top_Trending_V6"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    results.append(_fail(name, f"HTTP {resp.status}"))
                else:
                    data = await resp.json(content_type=None)
                    payload_ok, detail = validate_charts_payload(data)
                    if payload_ok:
                        results.append(_ok(name, detail))
                    else:
                        results.append(_warn(name, detail))
        except Exception as exc:
            results.append(_fail(name, repr(exc)))

    # 4. Growth history store — the bot's own snapshots, which replaced the
    #    former third-party history API. No network call: this asserts the
    #    local store is actually filling up, since a silently empty
    #    game_history would mean every embed loses its growth figures.
    try:
        import history as _history

        snapshots = _history.count_snapshots()
        if snapshots >= 2:
            results.append(_ok("Growth history", f"{snapshots:,} snapshots stored"))
        else:
            results.append(_warn(
                "Growth history",
                f"{snapshots} snapshot(s) so far — expected on a fresh install. "
                "A game shows growth once it has been scanned twice.",
            ))
    except Exception as exc:
        results.append(_fail("Growth history", repr(exc)))

    # 5. AI provider (key presence only — no network call)
    results.append(_ai_status())

    # 6. Optional extras: Discord token presence (no network call)
    if not os.environ.get("DISCORD_TOKEN"):
        results.append(_fail("Discord token", "DISCORD_TOKEN not set"))
    else:
        results.append(_ok("Discord token"))

    await session.close()

    LAST_RESULTS = results
    for name, status, detail in results:
        line = f"health: {status} {name}"
        if detail:
            line += f" — {detail}"
        (logger.warning if status == "⚠️" else logger.info if status == "✅" else logger.error)(line)

    return results


def format_health_results(results=None) -> str:
    """Render results for the /status command."""
    results = LAST_RESULTS if results is None else results
    if not results:
        return "Health check has not run yet."
    lines = [f"{status} **{name}**" + (f" — {detail}" if detail else "") for name, status, detail in results]
    broken = sum(1 for _, s, _ in results if s == "❌")
    warned = sum(1 for _, s, _ in results if s == "⚠️")
    parts = []
    if broken:
        parts.append(f"{broken} dependency(ies) failing")
    if warned:
        parts.append(f"{warned} warning(s)")
    if parts:
        header = f"⚠️ {', '.join(parts)}:"
    else:
        header = "✅ All dependencies healthy:"
    return header + "\n" + "\n".join(lines)
