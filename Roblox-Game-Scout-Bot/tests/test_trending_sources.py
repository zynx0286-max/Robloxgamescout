import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Platforms the bot used to link to. Their discovery functions were always
# empty (no public game-list API) and Atlas was the last third-party data
# dependency, so none of them should reappear in an embed's link row.
REMOVED_PLATFORMS = ("RoMonitor", "Creator Exchange", "BloxAtlas", "Atlas")


def test_build_analytics_links_is_roblox_only():
    from trending_sources import build_analytics_links

    links = build_analytics_links(994732206)

    assert "Roblox" in links
    assert "roblox.com/games/994732206" in links["Roblox"]
    for platform in REMOVED_PLATFORMS:
        assert platform not in links


def test_build_analytics_links_handles_missing_id():
    from trending_sources import build_analytics_links

    assert build_analytics_links(None) == {}
    assert build_analytics_links(0) == {}


def test_removed_scrapers_are_gone_from_the_module():
    import trending_sources

    assert not hasattr(trending_sources, "get_romonitor_games")
    assert not hasattr(trending_sources, "get_creator_exchange_games")
    # The trending entry point still exists under its Roblox-native name.
    assert hasattr(trending_sources, "get_charts_trending")


def test_retired_sort_logs_a_warning_not_silence(tmp_path, monkeypatch, caplog):
    """A non-200 sort response must be a visible WARNING.

    Roblox answers 400 with "Sort not found or not enabled" when it retires
    or renames a sort id. The old DEBUG-level log meant a dead sort shrank
    discovery and the chart-rank signal with nothing but a debug line.
    """
    import logging
    import aiohttp
    import asyncio
    from aiohttp import web
    from trending_sources import get_roblox_charts_games

    async def dead_sort(request):
        return web.json_response(
            {"errors": [{"code": 10010, "message": "Sort not found or not enabled"}]},
            status=400,
        )

    app = web.Application()
    app.router.add_get("/explore-api/v1/get-sort-content", dead_sort)
    runner = web.AppRunner(app)

    async def _run():
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            monkeypatch.setattr(
                "trending_sources._EXPLORE_API", f"http://127.0.0.1:{port}/explore-api/v1"
            )
            async with aiohttp.ClientSession() as session:
                return await get_roblox_charts_games(
                    session, sorts=[("Dead_Sort_V9", "Dead Sort")]
                )
        finally:
            await runner.cleanup()

    with caplog.at_level(logging.WARNING, logger="trending_sources"):
        games = asyncio.run(_run())

    assert games == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "Dead_Sort_V9" in r.getMessage() and "400" in r.getMessage()
        for r in warnings
    )


def test_get_roblox_charts_games_returns_known_sort_ids():
    import aiohttp
    import asyncio
    from trending_sources import get_roblox_charts_games

    async def _run():
        async with aiohttp.ClientSession() as session:
            return await get_roblox_charts_games(
                session,
                max_per_sort=10,
                sorts=[("CCU_Based_V1", "Top Playing Now")],
            )

    games = asyncio.run(_run())

    assert isinstance(games, list)
    if games:
        assert "id" in games[0]
        assert games[0]["source"] == "RobloxCharts"
