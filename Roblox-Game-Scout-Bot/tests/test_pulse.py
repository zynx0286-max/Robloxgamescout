"""Pulse: the bot's own market read, on demand.

Contract under test (pulse.py + the digest integration):

* `/pulse` and the digest's pulse section are PURE LOCAL reads — no
  Roblox requests, instant, `/status`-safe.
* The three selectors (movers / climbers / radar catches) return plain
  dicts with names resolved from the bot's own records.
* Freshness gate: a stale `game_history` latest snapshot is never
  presented as current movement.
* Every renderer and the composer handle empty input with explicit
  quiet lines — never a blank section, never a crash.
* The digest keeps reporting when the pulse extension fails.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import pulse
import scheduler

_db_path = os.path.join(tempfile.mkdtemp(), "pulse-test.db")


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(pulse, "DATABASE_PATH", _db_path)
    database.create_database()
    yield _db_path


def _insert_alert(game_id, name, hours_ago=0):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT OR REPLACE INTO alerted_games (game_id, game_name, first_alerted) "
        "VALUES (?, ?, ?)",
        (game_id, name, ts),
    )
    conn.commit()
    conn.close()


def _insert_watch(game_id, name):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name, source) "
        "VALUES (0, ?, ?, 'auto')",
        (game_id, name),
    )
    conn.commit()
    conn.close()


def _insert_history(game_id, snapshots):
    """snapshots: list of (players, visits, offset-string)."""
    conn = sqlite3.connect(_db_path)
    for players, visits, offset in snapshots:
        conn.execute(
            "INSERT INTO game_history (game_id, players, visits, timestamp) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (game_id, players, visits, offset),
        )
    conn.commit()
    conn.close()


def _insert_chart_position(game_id, sort_id, sort_label, position, hours_ago,
                           cycle_ts=None):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    conn.execute(
        "INSERT INTO chart_positions "
        "(game_id, sort_id, sort_label, position, recorded_at, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (game_id, sort_id, sort_label, position, ts, cycle_ts or ts),
    )
    conn.commit()
    conn.close()


def _insert_ascent(game_id, name, kind, ccu, minutes_ago):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO ascent_state "
        "(game_id, game_name, last_kind, last_ccu, first_seen_ts, last_alert_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (game_id, name, kind, ccu, ts, ts),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

def test_top_movers_returns_fresh_rising_games(fresh_db):
    _insert_alert(1, "Rocket")
    _insert_history(1, [
        (100, 1000, "-3 hours"),
        (120, 1100, "-1 hour"),
        (180, 1300, "-2 minutes"),
    ])

    movers = pulse.top_movers()

    assert len(movers) == 1
    m = movers[0]
    assert m["game_id"] == 1
    assert m["game_name"] == "Rocket"  # resolved from alerted_games
    assert m["first_players"] == 100
    assert m["last_players"] == 180
    assert m["growth_pct"] == 80.0
    assert m["snapshot_count"] == 3


def test_top_movers_ignores_stale_latest_snapshot(fresh_db):
    """A tall snapshot from hours ago is not current movement."""
    _insert_history(2, [
        (100, 1000, "-22 hours"),
        (110, 1050, "-10 hours"),
        (900, 5000, "-5 hours"),
    ])

    assert pulse.top_movers() == []


def test_top_movers_ignores_falling_games(fresh_db):
    _insert_history(3, [
        (500, 2000, "-2 hours"),
        (400, 1500, "-5 minutes"),
    ])

    assert pulse.top_movers() == []


def test_top_movers_ignores_zero_or_unknown_baselines(fresh_db):
    _insert_history(4, [(0, 0, "-2 hours"), (500, 3000, "-5 minutes")])
    _insert_history(5, [(300, 1000, "-20 minutes")])  # only 1 snapshot

    assert pulse.top_movers() == []


def test_top_movers_resolves_name_from_watchlist(fresh_db):
    _insert_watch(6, "Watched Game")
    _insert_history(6, [(50, 500, "-1 hour"), (150, 600, "-3 minutes")])

    movers = pulse.top_movers()
    assert movers[0]["game_name"] == "Watched Game"


def test_top_movers_unknown_name_falls_back(fresh_db):
    _insert_history(7, [(80, 500, "-1 hour"), (240, 600, "-3 minutes")])

    movers = pulse.top_movers()
    assert movers[0]["game_name"] == "Game 7"


def test_top_chart_climbers_detects_cycle_over_cycle_climb(fresh_db):
    _insert_alert(10, "Climber")
    # Two cycles, same sort, position 30 → 8 (delta = 22, climbed).
    _insert_chart_position(10, "up-and-coming", "Up & Coming", 30, hours_ago=3)
    _insert_chart_position(10, "up-and-coming", "Up & Coming", 8, hours_ago=1)

    climbers = pulse.top_chart_climbers()
    assert len(climbers) == 1
    c = climbers[0]
    assert c["game_id"] == 10
    assert c["game_name"] == "Climber"
    assert c["prev_position"] == 30
    assert c["position"] == 8
    assert c["delta"] == 22
    assert "Up" in c["sort_label"]


def test_top_chart_climbers_ignores_falling_games(fresh_db):
    _insert_chart_position(11, "top", "Top", 5, hours_ago=2)
    _insert_chart_position(11, "top", "Top", 40, hours_ago=1)

    assert pulse.top_chart_climbers() == []


def test_top_chart_climbers_requires_two_distinct_cycles(fresh_db):
    """Two rows of the SAME cycle are not a baseline/movement pair."""
    cycle = "2026-01-01 06:00:00.000000"
    _insert_chart_position(12, "top", "Top", 5, hours_ago=1, cycle_ts=cycle)
    _insert_chart_position(12, "trending", "Trending", 3, hours_ago=1, cycle_ts=cycle)

    assert pulse.top_chart_climbers() == []


def test_top_chart_climbers_picks_best_delta_per_game(fresh_db):
    _insert_chart_position(13, "a", "Sort A", 50, hours_ago=2, cycle_ts="2026-01-01 06:00:00.000000")
    _insert_chart_position(13, "a", "Sort A", 45, hours_ago=1, cycle_ts="2026-01-01 10:00:00.000000")
    _insert_chart_position(13, "b", "Sort B", 20, hours_ago=2, cycle_ts="2026-01-01 06:00:00.000000")
    _insert_chart_position(13, "b", "Sort B", 3, hours_ago=1, cycle_ts="2026-01-01 10:00:00.000000")

    climbers = pulse.top_chart_climbers()
    assert len(climbers) == 1
    assert climbers[0]["delta"] == 17  # sort b is the better climb
    assert climbers[0]["sort_label"] == "Sort B"


def test_recent_ascent_events(fresh_db):
    _insert_ascent(20, "Radar Hit", "first", 300, minutes_ago=10)
    _insert_ascent(21, "Older Hit", "escalate", 700, minutes_ago=23 * 60)

    events = pulse.recent_ascent_events()
    assert len(events) == 2
    assert events[0]["game_id"] == 20  # newest first
    assert events[0]["kind"] == "first"
    assert events[1]["kind"] == "escalate"


def test_recent_ascent_events_window_excludes_old_posts(fresh_db):
    _insert_ascent(22, "Too Old", "first", 100, minutes_ago=25 * 60)
    assert pulse.recent_ascent_events() == []


# ---------------------------------------------------------------------------
# Renderers + composer
# ---------------------------------------------------------------------------

def test_render_movers():
    text = pulse.render_movers([{
        "game_name": "Rocket", "growth_pct": 80.0,
        "first_players": 100, "last_players": 180, "snapshot_count": 3,
    }])
    assert len(text) == 1
    assert "Rocket" in text[0] and "80" in text[0]


def test_renderers_and_composer_handle_empty_data():
    assert pulse.render_movers([]) == []
    assert pulse.render_climbers([]) == []
    assert pulse.render_ascents([]) == []
    body = pulse.compose_pulse_report([], [], [])
    assert "Market pulse" in body
    assert "No fresh CCU movement" in body
    assert "No chart climbs recorded" in body
    assert "Radar quiet" in body


def test_compose_pulse_report_full():
    body = pulse.compose_pulse_report(
        [{"game_name": "Rocket", "growth_pct": 80.0,
          "first_players": 100, "last_players": 180, "snapshot_count": 3}],
        [{"game_name": "Climber", "sort_label": "Up & Coming",
          "prev_position": 30, "position": 8, "delta": 22}],
        [{"game_name": "Radar Hit", "kind": "first", "ccu": 300}],
    )
    assert "Rocket" in body
    assert "Climber" in body
    assert "Radar Hit" in body
    assert "🚀 New ascent" in body


def test_pulse_report_fail_soft_on_missing_tables(tmp_path):
    """A corrupt or empty database contributes empty sections and text."""
    # A valid empty database: all sections should render as quiet lines.
    import config as cfg
    path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS game_history (id INTEGER PRIMARY KEY, game_id INTEGER, players INTEGER, visits INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS chart_positions (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL, sort_id TEXT NOT NULL, sort_label TEXT, position INTEGER NOT NULL, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, cycle_ts TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ascent_state (game_id INTEGER PRIMARY KEY, game_name TEXT, last_kind TEXT, last_ccu INTEGER, first_seen_ts TEXT, last_alert_ts TEXT)")
    conn.commit()
    conn.close()

    report = pulse.pulse_report(path)
    assert "Market pulse" in report


# ---------------------------------------------------------------------------
# Digest integration
# ---------------------------------------------------------------------------

def _reset_digest_state():
    scheduler._last_digest_date = None


def test_digest_includes_pulse_lines(fresh_db, monkeypatch):
    _insert_alert(30, "Rocket Digest")
    _insert_history(30, [(100, 1000, "-2 hours"), (200, 1500, "-3 minutes")])
    _insert_alert(31, "Old Game", hours_ago=48)
    _insert_ascent(32, "Radar Big", "first", 500, minutes_ago=15)

    message = scheduler.build_digest_message()

    assert "Market pulse" not in message  # the digest uses its own heading
    assert "📊 **Pulse" in message
    assert "Rocket Digest" in message
    assert "Radar Big" in message


def test_digest_pulse_section_survives_a_selector_failure(fresh_db, monkeypatch):
    """A broken selector logs and degrades — digest still posts."""
    # Simulate a corrupt DB by removing the game_history table mid-flight.
    conn = sqlite3.connect(_db_path)
    conn.execute("DROP TABLE IF EXISTS game_history")
    conn.commit()
    conn.close()

    message = scheduler.build_digest_message()  # must not raise
    assert "Daily digest" in message or "Good morning" in message

# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------

def test_pulse_command_is_registered():
    """setup_commands must register /pulse so it shows up in Discord."""
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert '@tree.command(' in source
    assert 'name="pulse"' in source
    assert 'pulse_report' in source  # real data path wired, not a stub reply
