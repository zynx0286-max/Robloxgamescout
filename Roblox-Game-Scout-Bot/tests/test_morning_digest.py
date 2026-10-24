"""Tests for the morning digest (build + DB helpers)."""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "digest-test.db")

import database
import scheduler

ET = ZoneInfo("America/New_York")


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    database.create_database()
    yield _db_path


def _insert_alert(game_id, name, hours_ago):
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


def _insert_scan_cycle(hours_ago):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO scan_log (event, message, timestamp) "
        "VALUES ('scan_complete', 'test', ?)",
        (ts,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def test_get_alerted_games_since_filters_old_rows(fresh_db):
    _insert_alert(1, "Fresh Game", hours_ago=2)
    _insert_alert(2, "Old Game", hours_ago=48)

    rows = database.get_alerted_games_since(hours=24)
    names = [r[0] for r in rows]
    assert names == ["Fresh Game"]


def test_get_alerted_games_since_respects_limit(fresh_db):
    for i in range(15):
        _insert_alert(100 + i, f"Game {i}", hours_ago=1)

    rows = database.get_alerted_games_since(hours=24, limit=10)
    assert len(rows) == 10


def test_count_alerted_since(fresh_db):
    _insert_alert(1, "A", hours_ago=1)
    _insert_alert(2, "B", hours_ago=3)
    _insert_alert(3, "C", hours_ago=72)

    assert database.count_alerted_since(hours=24) == 2
    assert database.count_alerted_since(hours=168) == 3


def test_count_scan_cycles_since(fresh_db):
    _insert_scan_cycle(hours_ago=1)
    _insert_scan_cycle(hours_ago=2)
    _insert_scan_cycle(hours_ago=72)
    # Non-scan events must not count.
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO scan_log (event, message, timestamp) "
        "VALUES ('scan_error', 'boom', datetime('now'))"
    )
    conn.commit()
    conn.close()

    assert database.count_scan_cycles_since(hours=24) == 2


# ---------------------------------------------------------------------------
# Digest message building (pure function, data injected)
# ---------------------------------------------------------------------------


def test_digest_message_with_games():
    rows = [("Blox Fruits", "2026-09-26 10:12:00"), ("Fisch", "2026-09-26 11:30:00")]
    text = scheduler.build_digest_message(
        alerted_rows=rows,
        alerts_count=2,
        cycles_count=22,
        now=datetime(2026, 9, 26, 10, 0, tzinfo=ET),
    )
    assert "Good morning" in text
    assert "Blox Fruits" in text
    assert "Fisch" in text
    assert "22" in text  # scan cycles
    assert "10:00–21:00 ET" in text  # default window


def test_digest_message_empty():
    text = scheduler.build_digest_message(
        alerted_rows=[], alerts_count=0, cycles_count=5
    )
    assert "No new games" in text


def test_digest_window_247_when_disabled(monkeypatch):
    monkeypatch.setattr(scheduler, "SCAN_ACTIVE_HOURS", False)
    text = scheduler.build_digest_message(
        alerted_rows=[], alerts_count=0, cycles_count=0
    )
    assert "24/7" in text


# ---------------------------------------------------------------------------
# Window function sanity (shared with /scanwindow)
# ---------------------------------------------------------------------------


def test_window_check_reports_inside_now(monkeypatch):
    # Freeze "now" to a mid-window ET time and confirm the public helper
    # used by /scanwindow agrees.
    fake_now = datetime(2026, 9, 26, 12, 0, tzinfo=ET)
    monkeypatch.setattr(scheduler, "SCAN_ACTIVE_HOURS", True)
    monkeypatch.setattr(scheduler, "SCAN_START_HOUR_ET", 10)
    monkeypatch.setattr(scheduler, "SCAN_END_HOUR_ET", 21)
    assert scheduler._within_active_window(fake_now) is True
