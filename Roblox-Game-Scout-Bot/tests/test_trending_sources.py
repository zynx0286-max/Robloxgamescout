import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_build_analytics_links_contains_all_platforms():
    from trending_sources import build_analytics_links

    links = build_analytics_links(994732206)

    assert "Roblox Charts" in links
    assert "RoMonitor" in links
    assert "Creator Exchange" in links
    assert "romonitorstats.com/experience/994732206" in links["RoMonitor"]
    assert "creatorexchange.io/game/994732206" in links["Creator Exchange"]


def test_build_analytics_links_handles_missing_id():
    from trending_sources import build_analytics_links

    assert build_analytics_links(None) == {}
    assert build_analytics_links(0) == {}


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
