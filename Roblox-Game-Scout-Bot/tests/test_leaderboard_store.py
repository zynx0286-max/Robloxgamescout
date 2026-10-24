"""Tests for the alerted_games leaderboard store.

mark_alerted now records the peak Scout Score the bot saw and how many
cycles the game has been re-discovered; top_alerted_by_scout_score reads
that store for /leaderboard. Legacy databases (no score columns) must
migrate in place without losing rows.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_path = os.path.join(tempfile.mkdtemp(), "leaderboard-test.db")

import database


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    yield _db_path


def _row(path, sql):
    conn = sqlite3.connect(path)
    row = conn.execute(sql).fetchone()
    conn.close()
    return row


def test_first_alert_records_score_and_count_one(fresh_db):
    database.mark_alerted(100, "Game A", scout_score=77)
    score, count = _row(
        fresh_db,
        "SELECT best_scout_score, alert_count FROM alerted_games WHERE game_id=100",
    )
    assert (score, count) == (77, 1)


def test_second_alert_keeps_the_highest_score(fresh_db):
    database.mark_alerted(100, "Game A", scout_score=55)
    database.mark_alerted(100, "Game A", scout_score=88)
    score, count = _row(
        fresh_db,
        "SELECT best_scout_score, alert_count FROM alerted_games WHERE game_id=100",
    )
    assert (score, count) == (88, 2)


def test_scoreless_alert_never_erases_a_recorded_score(fresh_db):
    database.mark_alerted(200, "Game B", scout_score=60)
    database.mark_alerted(200, "Game B")  # legacy caller without a score
    score, count = _row(
        fresh_db,
        "SELECT best_scout_score, alert_count FROM alerted_games WHERE game_id=200",
    )
    assert (score, count) == (60, 2)


def test_first_alerted_timestamp_survives_re_alerts(fresh_db):
    first = "2026-01-01 00:00:00"
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name, first_alerted) "
        "VALUES (300, 'Legacy Game', ?)", (first,)
    )
    conn.commit()
    conn.close()
    database.mark_alerted(300, "Legacy Game", scout_score=70)
    ts = _row(fresh_db, "SELECT first_alerted FROM alerted_games WHERE game_id=300")
    assert ts == (first,)


def test_leaderboard_orders_by_score_then_recency(fresh_db):
    database.mark_alerted(1, "Low", scout_score=40)
    database.mark_alerted(2, "High", scout_score=95)
    database.mark_alerted(3, "Mid", scout_score=70)
    rows = database.top_alerted_by_scout_score(limit=10, days=30)
    ids = [r[0] for r in rows]
    assert ids == [2, 3, 1]
    assert rows[0][2] == 95


def test_leaderboard_window_excludes_stale_rows(fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name, best_scout_score, "
        "alert_count, first_alerted) VALUES (400, 'Old Game', 100, 1, "
        "datetime('now', '-90 days'))"
    )
    conn.commit()
    conn.close()
    database.mark_alerted(401, "New Game", scout_score=50)

    rows_30 = database.top_alerted_by_scout_score(limit=10, days=30)
    assert [r[0] for r in rows_30] == [401]

    rows_all = database.top_alerted_by_scout_score(limit=10, days=0)
    assert {r[0] for r in rows_all} == {400, 401}


def test_legacy_table_migrates_without_losing_rows(tmp_path, monkeypatch):
    legacy = os.path.join(str(tmp_path), "legacy-alerts.db")
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE alerted_games ("
        "game_id INTEGER PRIMARY KEY, game_name TEXT, "
        "first_alerted TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (7, 'Ancient')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DATABASE", legacy)
    database.create_database()

    # Row survives; leaderboard columns exist; a fresh scored alert works.
    name = _row(legacy, "SELECT game_name FROM alerted_games WHERE game_id=7")
    assert name == ("Ancient",)
    database.mark_alerted(7, "Ancient", scout_score=65)
    score = _row(legacy, "SELECT best_scout_score FROM alerted_games WHERE game_id=7")
    assert score == (65,)
