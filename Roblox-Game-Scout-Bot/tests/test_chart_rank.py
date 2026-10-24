"""Chart-rank signal: positions, deltas, best rank, and the score component.

Roblox Charts is not just a discovery source — *where* a game sits on a
chart and whether it is climbing is one of the strongest early acquisition
signals. These tests pin:

  * cycle separation — a cycle's rows are never their own delta baseline;
  * a game on several charts reports its best position;
  * the delta is positive when the game CLIMBED;
  * the Scout Score bands and the climb bonus;
  * the rank survives the whole pipeline: discovery stub -> details merge
    -> score/embed fields.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chart_rank
import config
import database


def _db(tmp_path, monkeypatch) -> str:
    """Point every module that owns DATABASE_PATH at a throwaway database."""
    import history

    db_path = str(tmp_path / "charts.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(history, "DATABASE_PATH", db_path)
    monkeypatch.setattr(chart_rank, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


def _rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------
# Cycle separation
# --------------------------------------------------------------------------

def test_current_cycle_is_never_its_own_baseline(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)

    chart_rank.record_positions([(1, "Top_Trending_V6", "Top Trending", 5)])
    game = {"id": 1, "chart_appearances": [("Top_Trending_V6", "Top Trending", 5)]}
    chart_rank.attach_rank_signal(game)

    # First appearance: nothing earlier to compare against.
    assert game["chart_rank"] == 5
    assert "chart_rank_delta" not in game


def test_delta_compares_against_the_previous_cycle(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)

    chart_rank.record_positions([(1, "Top_Trending_V6", "Top Trending", 8)])
    chart_rank.record_positions([(1, "Top_Trending_V6", "Top Trending", 3)])

    game = {"id": 1, "chart_appearances": [("Top_Trending_V6", "Top Trending", 3)]}
    chart_rank.attach_rank_signal(game)

    # 8 -> 3 = climbed 5 spots; positive delta means climbed.
    assert game["chart_rank"] == 3
    assert game["chart_rank_delta"] == 5
    assert game["chart_best"] == 3


def test_a_falling_game_has_a_negative_delta(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    chart_rank.record_positions([(1, "CCU_Based_V1", "Top Playing Now", 2)])
    chart_rank.record_positions([(1, "CCU_Based_V1", "Top Playing Now", 9)])

    game = {"id": 1, "chart_appearances": [("CCU_Based_V1", "Top Playing Now", 9)]}
    chart_rank.attach_rank_signal(game)

    assert game["chart_rank_delta"] == -7
    # Best-ever survives the fall.
    assert game["chart_best"] == 2


def test_best_position_spans_all_cycles_and_sorts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    chart_rank.record_positions([
        (1, "Top_Trending_V6", "Top Trending", 40),
        (1, "CCU_Based_V1", "Top Playing Now", 12),
    ])
    chart_rank.record_positions([(1, "Top_Trending_V6", "Top Trending", 20)])

    assert chart_rank.best_position(1) == 12


# --------------------------------------------------------------------------
# attach_rank_signal
# --------------------------------------------------------------------------

def test_best_appearance_wins_across_sorts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    game = {
        "id": 1,
        "chart_appearances": [
            ("Top_Trending_V6", "Top Trending", 3),
            ("CCU_Based_V1", "Top Playing Now", 80),
        ],
    }
    chart_rank.attach_rank_signal(game)

    assert game["chart_rank"] == 3
    assert game["charts_sort"] == "Top Trending"


def test_single_rank_stub_without_appearances_still_works(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    game = {
        "id": 1,
        "chart_rank": 7,
        "charts_sort": "Up-and-Coming",
        "charts_sort_id": "Up_And_Coming_V6",
    }
    chart_rank.attach_rank_signal(game)

    assert game["chart_rank"] == 7
    assert game["charts_sort"] == "Up-and-Coming"


def test_uncharted_games_are_left_untouched(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    game = {"id": 1, "name": "Not Charted"}
    chart_rank.attach_rank_signal(game)

    assert "chart_rank" not in game
    assert "chart_rank_delta" not in game
    assert "chart_best" not in game


# --------------------------------------------------------------------------
# Scout Score component
# --------------------------------------------------------------------------

def test_chart_score_bands():
    assert chart_rank.chart_score({"chart_rank": 1}) == 10
    assert chart_rank.chart_score({"chart_rank": 3}) == 10
    assert chart_rank.chart_score({"chart_rank": 10}) == 8
    assert chart_rank.chart_score({"chart_rank": 25}) == 6
    assert chart_rank.chart_score({"chart_rank": 50}) == 4
    assert chart_rank.chart_score({"chart_rank": 90}) == 2


def test_chart_score_climb_bonus():
    # Climbing 5+ spots earns +2, capped at 10.
    assert chart_rank.chart_score(
        {"chart_rank": 30, "chart_rank_delta": 5}
    ) == 6
    assert chart_rank.chart_score(
        {"chart_rank": 3, "chart_rank_delta": 8}
    ) == 10
    # A fall earns nothing extra.
    assert chart_rank.chart_score(
        {"chart_rank": 30, "chart_rank_delta": -3}
    ) == 4


def test_chart_score_zero_when_uncharted():
    assert chart_rank.chart_score({}) == 0
    assert chart_rank.chart_score({"chart_rank": None}) == 0
    assert chart_rank.chart_score({"chart_rank": "nonsense"}) == 0


def test_chart_score_text_formats_movement():
    text = chart_rank.chart_score_text(
        {"chart_rank": 3, "charts_sort": "Top Trending", "chart_rank_delta": 4,
         "chart_best": 1}
    )
    assert "#3 on Top Trending" in text
    assert "▲4" in text
    assert "best #1" in text

    flat = chart_rank.chart_score_text(
        {"chart_rank": 12, "charts_sort": "Top Playing Now"}
    )
    assert "#12 on Top Playing Now" in flat

    assert chart_rank.chart_score_text({}) == "📊 Not on a chart this cycle"


def test_scout_score_includes_the_chart_component(monkeypatch):
    import scanner

    async def fake_dev(game):
        return 0, "👨‍💻 Developer", "unknown"

    monkeypatch.setattr(scanner, "calculate_developer_score", fake_dev)

    charted = asyncio.run(scanner.calculate_scout_score({
        "playing": 500, "visits": 50_000, "favorites": 1_000,
        "growth": 0, "created": None,
        "chart_rank": 3, "charts_sort": "Top Trending",
    }))
    labels = [label for label, _, _ in charted["breakdown"]]
    assert "📊 Chart rank" in labels
    chart_pts = dict((label, pts) for label, pts, _ in charted["breakdown"])
    assert chart_pts["📊 Chart rank"] == 10

    uncharted = asyncio.run(scanner.calculate_scout_score({
        "playing": 500, "visits": 50_000, "favorites": 1_000,
        "growth": 0, "created": None,
    }))
    chart_pts = dict((label, pts) for label, pts, _ in uncharted["breakdown"])
    assert chart_pts["📊 Chart rank"] == 0


# --------------------------------------------------------------------------
# Pipeline carry-through
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Serves queued payloads to consecutive GETs, like the chart sorts."""

    def __init__(self, payloads):
        self._payloads = list(payloads)

    def get(self, url, **kwargs):
        return _FakeResponse(self._payloads.pop(0))


def test_chart_stubs_carry_rank_and_merge_across_sorts():
    from trending_sources import get_roblox_charts_games

    session = _FakeSession([
        {"games": [
            {"universeId": 1, "name": "Charted", "playerCount": 50,
             "totalUpVotes": 10, "totalDownVotes": 1, "genreL1": "Adventure",
             "rootPlaceId": 11},
            {"universeId": 2, "name": "Second", "playerCount": 40,
             "totalUpVotes": 9, "totalDownVotes": 1, "genreL1": "Adventure",
             "rootPlaceId": 22},
        ]},
        {"games": [
            {"universeId": 1, "name": "Charted", "playerCount": 50,
             "totalUpVotes": 10, "totalDownVotes": 1, "genreL1": "Adventure",
             "rootPlaceId": 11},
        ]},
    ])

    games = asyncio.run(get_roblox_charts_games(
        session, sorts=[("Sort_A_V1", "Sort A"), ("Sort_B_V1", "Sort B")]
    ))

    assert len(games) == 2
    first = games[0]
    assert first["chart_rank"] == 1          # position within its sort
    assert first["charts_sort_id"] == "Sort_A_V1"
    assert ("Sort_A_V1", "Sort A", 1) in first["chart_appearances"]
    assert ("Sort_B_V1", "Sort B", 1) in first["chart_appearances"]
    # The second game keeps the position it had in its own sort.
    assert games[1]["chart_rank"] == 2


def test_details_merge_carries_chart_fields(monkeypatch):
    import scanner

    stubs = [{
        "id": 1, "source": "RobloxCharts", "on_charts": True,
        "charts_sort": "Top Trending", "charts_sort_id": "Top_Trending_V6",
        "chart_rank": 4,
        "chart_appearances": [("Top_Trending_V6", "Top Trending", 4)],
    }]

    async def fake_batch(session, ids):
        return [{"id": 1, "name": "Charted", "playing": 10, "visits": 5}]

    monkeypatch.setattr(scanner, "fetch_game_details_batch", fake_batch)

    enriched = asyncio.run(scanner._fetch_game_details_async(None, stubs))

    assert enriched[0]["chart_rank"] == 4
    assert enriched[0]["charts_sort_id"] == "Top_Trending_V6"
    assert enriched[0]["chart_appearances"] == [
        ("Top_Trending_V6", "Top Trending", 4)
    ]


def test_discovery_records_chart_positions(tmp_path, monkeypatch):
    """data_collector is where positions hit the store, once per cycle."""
    import data_collector

    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(data_collector, "ENABLE_ROBLOX_CHARTS", True)

    async def fake_search(*a, **k):
        return []

    async def fake_new(*a, **k):
        return []

    def fake_seeds():
        return []

    async def fake_charts(session):
        return [
            {"id": 1, "name": "Charted", "on_charts": True,
             "chart_appearances": [("Sort_A_V1", "Sort A", 1)]},
            {"id": 2, "name": "Also Charted", "on_charts": True,
             "chart_appearances": [("Sort_A_V1", "Sort A", 2)]},
        ]

    monkeypatch.setattr(data_collector, "multi_keyword_search", fake_search)
    monkeypatch.setattr(data_collector, "search_new_games", fake_new)
    monkeypatch.setattr(data_collector, "get_curated_seeds", fake_seeds)
    monkeypatch.setattr(data_collector, "get_charts_trending", fake_charts)

    games = asyncio.run(data_collector.collect_games_async(session=None))

    assert len(games) == 2
    assert all(g["on_charts"] for g in games)
    rows = _rows(
        db,
        "SELECT game_id, sort_id, sort_label, position FROM chart_positions "
        "ORDER BY position",
    )
    assert rows == [
        (1, "Sort_A_V1", "Sort A", 1),
        (2, "Sort_A_V1", "Sort A", 2),
    ]


def test_chart_fields_survive_dedupe_when_search_wins(tmp_path, monkeypatch):
    """A game found first by keyword search must still carry its rank.

    Dedupe keeps the FIRST stub per id, so a chart game that search also
    found arrives as the search stub — and used to reach the filters with
    on_charts=True and no rank fields at all.
    """
    import data_collector

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(data_collector, "ENABLE_ROBLOX_CHARTS", True)

    async def fake_search(*a, **k):
        return [{"id": 7, "name": "Search First", "source": "RobloxSearch"}]

    async def fake_new(*a, **k):
        return []

    def fake_seeds():
        return []

    async def fake_charts(session):
        return [{
            "id": 7, "name": "Search First", "source": "RobloxCharts",
            "charts_sort": "Sort A", "charts_sort_id": "Sort_A_V1",
            "chart_rank": 3,
            "chart_appearances": [("Sort_A_V1", "Sort A", 3)],
        }]

    monkeypatch.setattr(data_collector, "multi_keyword_search", fake_search)
    monkeypatch.setattr(data_collector, "search_new_games", fake_new)
    monkeypatch.setattr(data_collector, "get_curated_seeds", fake_seeds)
    monkeypatch.setattr(data_collector, "get_charts_trending", fake_charts)

    games = asyncio.run(data_collector.collect_games_async(session=None))

    assert len(games) == 1
    merged = games[0]
    assert merged["on_charts"] is True
    assert merged["chart_rank"] == 3
    assert merged["charts_sort"] == "Sort A"
    assert merged["chart_appearances"] == [("Sort_A_V1", "Sort A", 3)]


def test_scanner_attaches_rank_fields_after_the_merge(tmp_path, monkeypatch):
    """The full seam: discovery wrote cycle 1, this cycle's stub carries
    rank 2, and the scanner's per-game processing resolves the delta."""
    import scanner

    db = _db(tmp_path, monkeypatch)

    async def fake_dev(game):
        return 0, "👨‍💻 Developer", "unknown"

    async def fake_batch(session, ids):
        return [{"id": 1, "name": "Charted", "playing": 10, "visits": 5}]

    monkeypatch.setattr(scanner, "calculate_developer_score", fake_dev)
    monkeypatch.setattr(scanner, "fetch_game_details_batch", fake_batch)

    # Two discovery passes: last cycle the game sat at #6, this cycle it is
    # at #2. The second record call is what makes the first one "previous".
    chart_rank.record_positions([(1, "Sort_A_V1", "Sort A", 6)])
    chart_rank.record_positions([
        (1, "Sort_A_V1", "Sort A", 2),
        (999, "Sort_A_V1", "Sort A", 1),  # another charted game, same cycle
    ])

    stubs = [{
        "id": 1, "source": "RobloxCharts", "on_charts": True,
        "charts_sort": "Sort A", "charts_sort_id": "Sort_A_V1",
        "chart_rank": 2,
        "chart_appearances": [("Sort_A_V1", "Sort A", 2)],
    }]

    enriched = asyncio.run(scanner._fetch_game_details_async(None, stubs))
    chart_rank.attach_rank_signal(enriched[0])

    assert enriched[0]["chart_rank"] == 2
    assert enriched[0]["chart_rank_delta"] == 4   # climbed from 6 to 2
    assert enriched[0]["chart_best"] == 2


# --------------------------------------------------------------------------
# latest_positions (tracker embeds)
# --------------------------------------------------------------------------

def test_latest_positions_returns_most_recent_per_sort(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)

    chart_rank.record_positions([
        (1, "Sort_A_V1", "Sort A", 9),
        (1, "Sort_B_V1", "Sort B", 4),
    ])
    chart_rank.record_positions([
        (1, "Sort_A_V1", "Sort A", 2),
    ])

    positions = chart_rank.latest_positions(1)

    # Sort A updated this cycle, Sort B still showing its last known spot.
    assert positions == [("Sort A", 2), ("Sort B", 4)]
    assert chart_rank.latest_positions(999) == []
    assert chart_rank.latest_positions("junk") == []


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def test_old_chart_positions_are_pruned(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db = _db(tmp_path, monkeypatch)
    chart_rank.record_positions([(1, "Sort_A_V1", "Sort A", 5)])

    # Age every row past the retention window, then force the prune.
    stale_cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(days=chart_rank.CHART_POSITIONS_RETENTION_DAYS + 1)
    ).isoformat(sep=" ")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE chart_positions SET recorded_at = ?", (stale_cutoff,))
    conn.commit()
    conn.close()

    chart_rank._last_chart_prune = None
    chart_rank._maybe_prune()

    assert _rows(db, "SELECT * FROM chart_positions") == []
