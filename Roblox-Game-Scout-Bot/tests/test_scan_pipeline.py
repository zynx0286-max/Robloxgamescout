"""Scan-pipeline ordering guarantees.

Two bugs lived in this seam and both produced the same symptom — a scan that
silently returned nothing:

  * the rating filter rejects any game without rating data, but ratings were
    only fetched *after* the scan (by the scheduler) had already returned;
  * chart membership is decided during discovery, but the batch normalizer
    builds fresh dicts, so the flag was dropped before the filter read it.

These tests pin the ordering so neither can come back.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner


DEFAULTS = {
    "minimum_visits": 0,
    "maximum_visits": 1_500_000,
    "minimum_players": 15,
    "maximum_players": 2500,
    "minimum_growth": 0,
    "minimum_rating": 75,
    "require_discord": False,
    "require_charts": False,
    "genre": "Any",
    "max_age": 0,
}


def test_details_merge_carries_chart_membership(monkeypatch):
    stubs = [
        {"id": 1, "source": "RobloxCharts", "on_charts": True,
         "charts_sort": "Top Trending"},
        {"id": 2, "source": "RobloxSearch", "on_charts": False},
    ]

    async def fake_batch(session, ids):
        return [
            {"id": 1, "name": "On A Chart", "playing": 10, "visits": 5},
            {"id": 2, "name": "Not On A Chart", "playing": 20, "visits": 6},
        ]

    monkeypatch.setattr(scanner, "fetch_game_details_batch", fake_batch)

    enriched = asyncio.run(scanner._fetch_game_details_async(None, stubs))

    assert enriched[0]["on_charts"] is True
    assert enriched[0]["charts_sort"] == "Top Trending"
    assert enriched[0]["source"] == "RobloxCharts"
    assert enriched[1]["on_charts"] is False
    # A search-only game has no chart to attribute it to.
    assert "charts_sort" not in enriched[1]


def _patch_pipeline(monkeypatch, *, rating=90.0):
    """Wire scan_games_async to a single fake candidate, with no network."""
    calls = {"ratings": 0}

    async def fake_collect(session, seed_ids=None, is_deep_scan=False):
        return [{"id": 1, "source": "Test", "on_charts": True}]

    async def fake_batch(session, ids):
        return [{
            "id": 1,
            "place_id": 11,
            "name": "Test Game",
            "playing": 100,
            "visits": 1000,
            "favorites": 10,
            "description": "",
            "created": None,
            "creator": "Someone",
            "genre": "Simulator",
        }]

    async def fake_ratings(session, games):
        calls["ratings"] += 1
        for game in games:
            game["rating_percent"] = rating
        return games

    async def fake_thumbnails(session, games):
        return games

    async def fake_discord(*args, **kwargs):
        return None

    async def fake_dev_score(game):
        return 0, "👨‍💻 Developer", "unknown"

    monkeypatch.setattr(scanner, "collect_games_async", fake_collect)
    monkeypatch.setattr(scanner, "fetch_game_details_batch", fake_batch)
    monkeypatch.setattr(scanner, "enrich_games_with_ratings", fake_ratings)
    monkeypatch.setattr(scanner, "enrich_games_with_thumbnails", fake_thumbnails)
    monkeypatch.setattr(scanner, "find_discord_invite", fake_discord)
    monkeypatch.setattr(scanner, "calculate_developer_score", fake_dev_score)
    monkeypatch.setattr(scanner, "save_game_snapshot", lambda info: None)
    monkeypatch.setattr(scanner, "get_growth", lambda game_id: 0)
    monkeypatch.setattr(scanner, "compute_velocity", lambda game_id: {})
    # The bulk read functions are the same seams (scanner's local-history
    # reads) under their new names; both forms must be isolated from the
    # real database.
    monkeypatch.setattr(scanner, "history_growth_bulk", lambda ids: {})
    monkeypatch.setattr(scanner, "compute_velocities_bulk", lambda ids: {})
    return calls


def test_ratings_are_attached_before_the_filters_run(monkeypatch):
    calls = _patch_pipeline(monkeypatch)

    results, diagnostics = asyncio.run(
        scanner.scan_games_async(
            session=None, user_settings=DEFAULTS, return_diagnostics=True
        )
    )

    assert calls["ratings"] == 1
    assert diagnostics["rating_passes"] == 1
    assert len(results) == 1


def test_an_unrated_game_is_rejected_rather_than_silently_dropped(monkeypatch):
    """With a rating floor in force, no rating data is a rejection — and the
    diagnostics must say so instead of the game just vanishing."""
    _patch_pipeline(monkeypatch, rating=0.0)

    results, diagnostics = asyncio.run(
        scanner.scan_games_async(
            session=None, user_settings=DEFAULTS, return_diagnostics=True
        )
    )

    assert diagnostics["rating_passes"] == 0
    assert results == []


def test_require_charts_filters_on_the_discovery_flag(monkeypatch):
    _patch_pipeline(monkeypatch)
    settings = dict(DEFAULTS, require_charts=True)

    results = asyncio.run(
        scanner.scan_games_async(session=None, user_settings=settings)
    )

    # The single fake candidate is on a chart, so it survives.
    assert len(results) == 1
