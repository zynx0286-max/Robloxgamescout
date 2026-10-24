"""Tests for the zero-dependency HTML pages on the portfolio API.

Contract: 200 + HTML rendered strictly from the same cached payloads
the JSON feeds serve; empty stores render explicit empty states, never
stray mock data; every interpolated name is HTML-escaped. The sparkline
pages (/dashboard cards, /games selector, /game/{id}) derive from the
bot's own snapshot stores — zero network, zero external assets.
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "dashboard-test.db")

import database
import portfolio_api
import portfolio_db


@pytest.fixture()
def seeded(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(portfolio_db, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    portfolio_db.create_portfolio_tables()
    portfolio_api._cache.clear()
    portfolio_api.web_ui._BENCH_CACHE.update(
        {"key": None, "at": 0.0, "series": {}}
    )
    yield _db_path
    # Teardown matters: this file's /dashboard call warms the module-global
    # ("portfolio", 0) cache entry that tests/test_portfolio_api.py (which
    # binds its zero-TTL *after* portfolio_api was first imported) would
    # otherwise still serve. Clearing keeps the two fixtures isolated.
    portfolio_api._cache.clear()


def _client():
    from fastapi.testclient import TestClient
    return TestClient(portfolio_api.app)


def test_dashboard_renders_leaderboard_and_cards(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    portfolio_db.add_portfolio_game(10, "Gold Game")
    portfolio_db.record_game_snapshot(10, 200, 5_000)

    resp = _client().get("/dashboard")

    assert resp.status_code == 200
    text = resp.text
    assert "text/html" in resp.headers["content-type"]
    assert "Gold Game" in text                    # card + leaderboard row
    assert "95" in text                            # best score
    assert "200 CCU" in text                       # live players
    assert "generated" in text


def test_dashboard_empty_store_renders_empty_states(seeded):
    resp = _client().get("/dashboard")
    assert resp.status_code == 200
    assert "No portfolio projects registered." in resp.text
    assert "No alerts recorded yet." in resp.text


def test_dashboard_escapes_game_names(seeded):
    database.mark_alerted(10, "<script>alert(1)</script>", scout_score=95)

    text = _client().get("/dashboard").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


# ---------------------------------------------------------------------------
# Sparkline rendering
# ---------------------------------------------------------------------------


def _hts(hours_ago):
    from datetime import datetime, timedelta, timezone
    ts = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=hours_ago)
    )
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def test_dashboard_has_auto_refresh_meta(seeded):
    text = _client().get("/dashboard").text
    assert 'http-equiv="refresh"' in text
    assert 'content="300"' in text


def test_games_selector_carries_sparklines_without_text_extraction(seeded):
    """Selector rows: one bulk game_history read serves every sparkline."""
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _history_row(10, 100, hours_ago=48)
    _history_row(10, 220, hours_ago=2)
    database.mark_alerted(20, "Quiet Game", scout_score=60)
    portfolio_api._cache.clear()

    text = _client().get("/games").text

    assert "<polyline" in text                    # Gold Game's 7d trend
    assert "no history yet" in text               # Quiet Game's explicit state


def test_game_page_links_its_json_series(seeded):
    text = _client().get("/game/10").text
    assert "/api/v1/series?ids=10&hours=168" in text
    assert "this series as JSON" in text


def _history_row(game_id, players, hours_ago):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (?, ?, ?, ?)",
        (game_id, players, players * 10, _hts(hours_ago)),
    )
    conn.commit()
    conn.close()


def test_sparkline_svg_shape_and_normalization():
    svg = portfolio_api._sparkline_svg([100, 150, 260])
    assert svg is not None
    assert "<svg" in svg and "<polyline" in svg and "<polygon" in svg
    # Points interpolated across the width; lowest value pinned at the bottom.
    assert svg.count(",") >= 2
    assert portfolio_api._sparkline_svg([]) is None
    assert portfolio_api._sparkline_svg([None]) is None


def test_dashboard_cards_carry_sparklines(seeded):
    portfolio_db.add_portfolio_game(10, "Gold Game")
    portfolio_db.record_game_snapshot(10, 200, 5_000)
    portfolio_db.record_game_snapshot(10, 260, 5_200)
    portfolio_api._cache.clear()  # payload rebuilt with the fresh series

    text = _client().get("/dashboard").text

    assert "<polyline" in text
    assert "No fresh 24h series yet." not in text


def test_games_selector_links_leaderboard_games(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    portfolio_api._cache.clear()

    text = _client().get("/games").text

    assert "Gold Game" in text
    assert "href='/game/10'" in text


def test_games_selector_escapes_names(seeded):
    database.mark_alerted(11, "<b>bold</b>", scout_score=80)
    portfolio_api._cache.clear()

    text = _client().get("/games").text

    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text


def test_game_page_renders_history_from_bot_store(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _history_row(10, 100, hours_ago=48)
    _history_row(10, 130, hours_ago=24)
    _history_row(10, 200, hours_ago=2)

    result = _client().get("/game/10")
    text = result.text

    assert result.status_code == 200
    assert "Gold Game" in text
    assert "<polyline" in text      # 3-point history series
    assert "200 CCU latest" in text
    assert "href='/games'" in text  # back-link


def test_game_page_empty_history_is_explicit(seeded):
    text = _client().get("/game/999").text
    assert "No snapshot history" in text
    assert "<polyline" not in text
    assert "Game 999" in text       # unknown name falls back to the id


# ---------------------------------------------------------------------------
# Console: front door, market sections, index metadata, detail signals
# ---------------------------------------------------------------------------


def test_root_redirects_to_the_board(seeded):
    result = _client().get("/", follow_redirects=False)
    assert result.status_code == 307
    assert result.headers["location"] == "/dashboard"


def test_dashboard_renders_every_market_section(seeded):
    text = _client().get("/dashboard").text
    for heading in ("Opportunity board", "Market pulse", "Chart climbers",
                    "Ascent radar", "Latest alerts", "Portfolio"):
        assert heading in text
    assert "No fresh movement yet" in text
    assert "Radar is quiet." in text


def test_dashboard_never_loads_external_assets(seeded):
    portfolio_db.add_portfolio_game(
        10, "Gold Game",
        thumbnail_url="https://cdn.example.com/thumbnail.png",
    )
    portfolio_db.record_game_snapshot(10, 200, 5_000)
    portfolio_api._cache.clear()

    text = _client().get("/dashboard").text

    assert "cdn.example.com" not in text   # monogram, never a remote image
    assert "<img" not in text
    assert 'src="http' not in text
    assert "@import" not in text
    assert "GG" in text                     # monogram fallback


def test_games_index_has_csv_export_and_compare_nav(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    portfolio_api._cache.clear()

    text = _client().get("/games").text

    assert "data-export" in text
    assert "Export CSV" in text
    assert 'href="/compare"' in text
    assert "press <b>/</b> to search" in text


def test_compare_renders_two_games_side_by_side(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    database.mark_alerted(20, "Silver Game", scout_score=70)
    _history_row(10, 100, hours_ago=48)
    _history_row(10, 200, hours_ago=2)
    _history_row(20, 300, hours_ago=48)
    _history_row(20, 150, hours_ago=2)
    portfolio_api._cache.clear()

    result = _client().get("/compare?a=10&b=20")
    text = result.text

    assert result.status_code == 200
    assert "Gold Game" in text and "Silver Game" in text
    assert "At a glance" in text
    assert text.count('class="compare-col"') == 2
    assert text.count('class="panel" data-chart') == 2
    assert "⇄ Swap" in text
    assert 'href="/compare?a=20&b=10"' in text
    assert "<polyline" in text
    assert "class='best'" in text          # the stronger side is marked


def test_compare_defaults_to_the_top_two(seeded):
    database.mark_alerted(10, "Best Game", scout_score=95)
    database.mark_alerted(20, "Second Game", scout_score=80)
    database.mark_alerted(30, "Third Game", scout_score=60)
    portfolio_api._cache.clear()

    text = _client().get("/compare").text

    assert '<option value="10" selected>' in text
    assert '<option value="20" selected>' in text
    assert '<option value="30" selected>' not in text


def test_compare_same_id_falls_back_to_another_game(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    database.mark_alerted(20, "Silver Game", scout_score=70)
    portfolio_api._cache.clear()

    text = _client().get("/compare?a=10&b=10").text

    assert '<option value="20" selected>' in text
    assert text.count('class="compare-col"') == 2


def test_compare_empty_store_is_explicit(seeded):
    text = _client().get("/compare").text

    assert "Nothing to compare yet." in text
    assert 'class="panel compare-form"' not in text


def test_compare_escapes_names(seeded):
    database.mark_alerted(10, "<script>alert(1)</script>", scout_score=95)
    database.mark_alerted(20, "Silver Game", scout_score=70)
    portfolio_api._cache.clear()

    text = _client().get("/compare").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_games_index_carries_filters_and_row_metadata(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _history_row(10, 220, hours_ago=2)
    portfolio_api._cache.clear()

    text = _client().get("/games").text

    assert 'data-index' in text
    assert 'data-filter="rising"' in text
    assert 'data-filter="falling"' in text
    assert 'data-filter="ai"' in text
    assert 'data-name="Gold Game"' in text
    assert 'data-score="95"' in text
    # The sparkline is an inline SVG, never a remote chart image.
    assert "<polyline" in text
    assert 'src="http' not in text


def test_game_page_shows_ai_verdict_and_chart_position(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _history_row(10, 120, hours_ago=48)
    _history_row(10, 240, hours_ago=2)
    _insert(
        "INSERT INTO chart_positions "
        "(game_id, sort_id, sort_label, position, recorded_at, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (10, "top-trending", "Top Trending", 4, _hts(1), "cycle-1"),
    )
    _insert(
        "INSERT INTO ai_analysis_cache "
        "(game_id, verdict, confidence, strengths, risks, recommendation, "
        "last_checked) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            10, "🟢 Worth Monitoring", 84,
            json.dumps(["Steady CCU growth", "Chart climb confirmed"]),
            json.dumps(["Crowded genre"]),
            "Validate retention before opening a conversation.",
            _hts(3),
        ),
    )
    portfolio_api._cache.clear()

    text = _client().get("/game/10").text

    assert "Worth Monitoring" in text
    assert "84%" in text
    assert "Steady CCU growth" in text
    assert "Top Trending" in text
    assert "#4" in text
    assert "240 CCU latest" in text


def test_game_page_opens_the_window_that_has_history(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=90)
    _history_row(10, 100, hours_ago=24 * 20)
    _history_row(10, 140, hours_ago=24 * 19)
    portfolio_api._cache.clear()

    text = _client().get("/game/10").text

    assert 'data-range="30d" aria-selected="true"' in text
    assert 'data-range="7d" aria-selected="false"' in text


def test_game_page_escapes_cached_ai_text(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _insert(
        "INSERT INTO ai_analysis_cache "
        "(game_id, verdict, confidence, strengths, risks, recommendation, "
        "last_checked) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            10, "🟡 Data Only", 40,
            json.dumps(["<script>alert(1)</script>"]),
            json.dumps(["<b>risk</b>"]),
            "<img src=x onerror=alert(1)>",
            _hts(1),
        ),
    )
    portfolio_api._cache.clear()

    text = _client().get("/game/10").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text
    assert "<img src=x" not in text


def test_game_page_scout_read_summarizes_momentum_and_position(seeded):
    _alert_row(10, "Gold Game", 95, hours_ago=24)
    _history_row(10, 100, hours_ago=24 * 6)
    _history_row(10, 200, hours_ago=1)

    text = _client().get("/game/10").text

    assert "Scout read" in text
    assert "Up 100.0% in 7 days, at its 30-day high." in text
    assert "Rising" in text
    assert "Scout alerted it at 95" in text
    assert "Best chart position" not in text       # no chart row seeded
    assert "No alert, chart position or watch" not in text


def test_game_page_scout_read_marks_a_stale_read_as_historical(seeded):
    _history_row(30, 200, hours_ago=24 * 5)
    _history_row(30, 120, hours_ago=10)

    text = _client().get("/game/30").text

    assert "Stale data" in text
    assert "newest snapshot arrived 10h ago" in text
    assert "treat this read as historical" in text
    assert "Down 40.0% in 7 days" in text


def test_game_page_signal_timeline_is_newest_first(seeded):
    _alert_row(10, "Gold Game", 95, hours_ago=24)
    _history_row(10, 100, hours_ago=24 * 6)
    _history_row(10, 200, hours_ago=1)
    _watch_row(10, "Gold Game", last_checked=_hts(0.5))
    _insert(
        "INSERT INTO chart_positions "
        "(game_id, sort_id, sort_label, position, recorded_at, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (10, "top-trending", "Top Trending", 4, _hts(3), "cycle-1"),
    )
    _insert(
        "INSERT INTO chart_positions "
        "(game_id, sort_id, sort_label, position, recorded_at, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (10, "top-earning", "Top Earning", 19, _hts(3), "cycle-1"),
    )
    _insert(
        "INSERT INTO ascent_state "
        "(game_id, game_name, last_kind, last_ccu, first_seen_ts, last_alert_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (10, "Gold Game", "first", 180, _hts(5), _hts(5)),
    )
    _insert(
        "INSERT INTO ai_analysis_cache "
        "(game_id, verdict, confidence, strengths, risks, recommendation, "
        "last_checked) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (10, "🟢 Worth Monitoring", 80, "[]", "[]", "Watch it.", _hts(2)),
    )

    text = _client().get("/game/10").text

    assert "Signal timeline" in text
    for label in ("Latest reading", "AI verdict stored", "Chart position",
                  "Radar first catch", "Alert fired", "Tracking started",
                  "First snapshot"):
        assert label in text

    order = [
        text.index(label) for label in (
            "Latest reading", "AI verdict stored", "Chart position",
            "Radar first catch", "Alert fired", "Tracking started",
            "First snapshot",
        )
    ]
    assert order == sorted(order)
    # Two charts recorded in the same cycle are two events, not one.
    assert "Top Trending · #4" in text and "Top Earning · #19" in text
    assert text.count("Chart position") >= 2


def test_game_page_without_history_has_explicit_read_and_timeline(seeded):
    text = _client().get("/game/999").text

    assert "No read yet" in text
    assert "There is no snapshot history" in text
    assert "No timeline events yet." in text


def test_benchmark_range_normalises_and_requires_three_games():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    raw = {
        1: [(now - timedelta(hours=20), 100.0),
            (now - timedelta(hours=2), 300.0)],
        2: [(now - timedelta(hours=20), 200.0),
            (now - timedelta(hours=2), 400.0)],
        3: [(now - timedelta(hours=20), 50.0),
            (now - timedelta(hours=2), 75.0)],
    }

    bench = portfolio_api.web_ui._benchmark_range(raw, 24, 1_000)

    assert bench["games"] == 3
    assert bench["growth"] == 100.0          # median ratios 100 -> 200
    assert bench["points"][0][1] == pytest.approx(1_000)
    assert bench["points"][-1][1] == pytest.approx(2_000)

    thin = portfolio_api.web_ui._benchmark_range(
        {1: raw[1], 2: raw[2]}, 24, 1_000
    )
    assert thin == {"points": [], "growth": None, "games": 0}


def test_game_page_overlays_the_tracked_market(seeded):
    for gid, name, base, last in (
        (10, "Gold Game", 100, 200),      # +100.0%
        (20, "Silver Game", 200, 300),    # +50.0%
        (30, "Bronze Game", 100, 150),    # +50.0%
    ):
        _alert_row(gid, name, 90, hours_ago=24)
        _history_row(gid, base, hours_ago=24 * 6)
        _history_row(gid, last, hours_ago=1)

    text = _client().get("/game/10").text

    assert "Scout market median" in text
    assert text.count('class="chart-bench"') >= 2   # wide + compact
    assert "Median CCU path of the games Scout has identified" in text
    assert "vs Scout market" in text
    assert "+50.0 pp" in text       # median market +50%, Gold +100%


def test_game_page_omits_the_benchmark_when_the_market_is_thin(seeded):
    _history_row(10, 100, hours_ago=24 * 6)
    _history_row(10, 200, hours_ago=1)

    text = _client().get("/game/10").text

    assert 'class="chart-bench"' not in text
    assert "Scout market median" not in text


def test_compare_charts_carry_the_market_benchmark(seeded):
    for gid, name, base, last in (
        (10, "Gold Game", 100, 200),
        (20, "Silver Game", 200, 300),
        (30, "Bronze Game", 100, 150),
    ):
        _alert_row(gid, name, 90, hours_ago=24)
        _history_row(gid, base, hours_ago=24 * 6)
        _history_row(gid, last, hours_ago=1)

    text = _client().get("/compare?a=10&b=20").text

    assert text.count("Scout market median") == 2
    assert text.count('class="chart-bench"') >= 2


def test_watchlist_empty_store_is_explicit(seeded):
    text = _client().get("/watchlist").text

    assert "No watched games yet." in text
    assert 'data-watchlist' in text
    assert "0 tracked" in text
    assert 'href="/watchlist"' in text


def test_watchlist_renders_momentum_and_staleness(seeded):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    _history_row(10, 50, hours_ago=48)
    _history_row(10, 100, hours_ago=10)
    _history_row(10, 200, hours_ago=1)
    _history_row(20, 300, hours_ago=48)
    _history_row(20, 250, hours_ago=2)
    _watch_row(10, "Gold Game", source="user", last_checked=_hts(0.2))
    _watch_row(10, "Gold Game", source="auto", last_checked=_hts(0.2))
    _watch_row(20, "Stale Game", source="user", last_checked=_hts(10))
    portfolio_api._cache.clear()

    text = _client().get("/watchlist").text

    assert "Gold Game" in text and "Stale Game" in text
    assert "Mine" in text and "Auto" in text
    assert "Spiking" in text and "Stale" in text
    assert "+100.0%" in text          # 100 -> 200 inside the 24h window
    assert text.count("data-watch data-id=") == 2
    assert "data-wsearch" in text
    assert 'data-wfilter="stale"' in text
    assert "data-wsort" in text


def test_watchlist_escapes_names(seeded):
    _watch_row(10, "<script>alert(1)</script>")

    text = _client().get("/watchlist").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_scorecard_empty_store_explains_the_horizon(seeded):
    text = _client().get("/scorecard").text

    assert "No calls to grade yet." in text
    assert "No graded calls yet." in text
    assert "No score bands to compare yet." in text
    assert '<circle class="dot' not in text


def test_scorecard_grades_72h_outcomes_and_bands(seeded):
    _alert_row(30, "Rocket Game", 95, hours_ago=120)
    _history_row(30, 100, hours_ago=121)
    _history_row(30, 130, hours_ago=48)
    _alert_row(40, "Faded Game", 85, hours_ago=120)
    _history_row(40, 200, hours_ago=121)
    _history_row(40, 150, hours_ago=48)
    _alert_row(50, "Flat Game", 75, hours_ago=120)
    _history_row(50, 300, hours_ago=121)
    _history_row(50, 310, hours_ago=48)
    _alert_row(60, "Too Soon", 88, hours_ago=24)
    _history_row(60, 100, hours_ago=25)
    _history_row(60, 120, hours_ago=1)
    portfolio_api._cache.clear()

    text = _client().get("/scorecard").text

    assert "Rocket Game" in text and "Faded Game" in text
    assert "+30.0%" in text and "-25.0%" in text
    assert "data-outcome=\"win\"" in text
    assert "data-outcome=\"miss\"" in text
    assert "data-outcome=\"quiet\"" in text
    assert "data-outcome=\"open\"" in text
    assert "33%" in text                     # one win out of three graded
    assert "of 4 alerts · 1 open" in text
    assert '<circle class="dot' in text        # calibration plot
    assert '"90+"' in text or ">90+<" in text
    assert 'href="/scorecard" aria-current="page"' in text


def test_scorecard_escapes_call_names(seeded):
    _alert_row(31, "<script>alert(1)</script>", 90, hours_ago=120)
    _history_row(31, 100, hours_ago=121)
    _history_row(31, 120, hours_ago=48)

    text = _client().get("/scorecard").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_record_grade_thresholds_are_fixed_and_honest():
    assert portfolio_api.web_ui._grade_record_call(100, 120)["status"] == "win"
    assert portfolio_api.web_ui._grade_record_call(100, 119.9)["status"] == "quiet"
    assert portfolio_api.web_ui._grade_record_call(100, 80)["status"] == "miss"
    assert portfolio_api.web_ui._grade_record_call(100, 80.1)["status"] == "quiet"
    assert portfolio_api.web_ui._grade_record_call(0, 100)["status"] == "unrateable"
    assert portfolio_api.web_ui._grade_record_call(100, None)["status"] == "unrateable"


# ---------------------------------------------------------------------------
# Market weather
# ---------------------------------------------------------------------------


def test_market_empty_store_is_explicit(seeded):
    text = _client().get("/market").text

    assert "No moves to chart yet." in text
    assert text.count("no fresh pair") == 2
    assert "No chart positions recorded" in text
    assert "No fresh gainers right now." in text
    assert "No fresh decliners right now." in text
    assert "No portfolio projects registered." in text
    assert 'href="/market" aria-current="page"' in text


def test_market_page_computes_breadth_and_never_shows_a_gainer_as_a_laggard(seeded):
    _history_row(10, 300, hours_ago=24 * 10)   # Gold's 30d peak
    _history_row(10, 100, hours_ago=5)
    _history_row(10, 200, hours_ago=1)          # +100.0%
    _history_row(20, 200, hours_ago=5)
    _history_row(20, 150, hours_ago=1)          # -25.0%
    _history_row(30, 100, hours_ago=5)
    _history_row(30, 100, hours_ago=1)          # flat
    _history_row(40, 100, hours_ago=5)
    _history_row(40, 103, hours_ago=1)          # +3.0%
    database.mark_alerted(10, "Gold Game", scout_score=95)
    portfolio_api._cache.clear()

    text = _client().get("/market").text

    assert "2 of 4 tracked games are up over 24 hours" in text
    assert "median move is +1.5%" in text
    assert "Two-way tape" in text
    assert "-25.0%" in text                    # the laggard's move
    assert "-33.3%" in text                    # Gold is 33.3% below its 30d peak

    leaders = text.split('data-market-movers="up"')[1]
    leaders, laggards = leaders.split('data-market-movers="down"')
    assert "Gold Game" in leaders
    assert "Game 40" in leaders
    assert "Game 20" in laggards
    # A rising game must never be listed under declines.
    assert "Game 40" not in laggards


def test_market_escapes_names(seeded):
    _history_row(10, 100, hours_ago=5)
    _history_row(10, 200, hours_ago=1)
    database.mark_alerted(10, "<script>alert(1)</script>", scout_score=90)

    text = _client().get("/market").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_market_verdict_thresholds_are_fixed():
    verdict = portfolio_api.web_ui._market_verdict
    assert verdict({"graded": 5, "up_pct": 70, "median": 4.0})["label"] == "Risk-on"
    assert verdict({"graded": 5, "up_pct": 58, "median": 1.0})["label"] == "Leaning up"
    assert verdict({"graded": 5, "up_pct": 30, "median": -4.0})["label"] == "Risk-off"
    assert verdict({"graded": 5, "up_pct": 40, "median": -1.0})["label"] == "Cooling"
    assert verdict({"graded": 5, "up_pct": 50, "median": 0.0})["label"] == "Two-way tape"
    assert verdict({"graded": 2, "up_pct": 100, "median": 8.0})["label"] == "Thin tape"
    assert verdict({"graded": 0, "up_pct": 0, "median": None})["label"] == "Thin tape"


def test_market_histogram_buckets_are_half_open():
    buckets = portfolio_api.web_ui._market_histogram(
        [-60, -50, -25, -10, 0, 10, 50, 75, None]
    )
    assert [b["count"] for b in buckets] == [1, 1, 1, 1, 1, 1, 0, 2]


def test_dashboard_links_to_the_market_read(seeded):
    text = _client().get("/dashboard").text
    assert 'href="/market"' in text
    assert "Full market read" in text


def _alert_row(game_id, name, score, hours_ago):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT OR REPLACE INTO alerted_games "
        "(game_id, game_name, first_alerted, best_scout_score, alert_count) "
        "VALUES (?, ?, ?, ?, 1)",
        (game_id, name, _hts(hours_ago), score),
    )
    conn.commit()
    conn.close()


def _watch_row(game_id, name, source="user", last_checked=None):
    _insert(
        "INSERT INTO watched_games "
        "(user_id, game_id, game_name, last_players, last_visits, "
        "last_checked, date_added, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1 if source == "user" else 0, game_id, name, 200, 5000,
            last_checked or _hts(0.1), _hts(72), source,
        ),
    )


def _insert(sql, params):
    conn = sqlite3.connect(_db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()
