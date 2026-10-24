"""Tests for game_history retention (index + pruning).

game_history grows by one row per scanned game per cycle; without a cap it
grows unboundedly (~14k rows/day at default settings).
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "history-test.db")

import database


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    database._last_history_prune = None  # force a prune window on first write
    yield _db_path


def _row_count(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM game_history")
    n = cur.fetchone()[0]
    conn.close()
    return n


def test_game_history_index_exists(fresh_db):
    conn = sqlite3.connect(fresh_db)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_game_history_game_time'")
    assert cur.fetchone() is not None
    conn.close()


def test_old_snapshots_are_pruned_on_write(fresh_db, monkeypatch):
    # Seed a snapshot that is far older than the 30-day retention window.
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (1, 10, 100, datetime('now', '-60 days'))"
    )
    conn.commit()
    conn.close()
    assert _row_count(fresh_db) == 1

    database.save_game_snapshot({"id": 2, "playing": 5, "visits": 50})

    assert _row_count(fresh_db) == 1  # old row pruned, new row kept


def test_recent_snapshots_survive_prune(fresh_db):
    database.save_game_snapshot({"id": 2, "playing": 5, "visits": 50})
    database.save_game_snapshot({"id": 2, "playing": 6, "visits": 60})
    assert _row_count(fresh_db) == 2


def test_prune_failure_does_not_break_snapshot_write(fresh_db, monkeypatch):
    real_connect = database.sqlite3.connect
    calls = {"n": 0}

    def failing_on_second_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # first = snapshot INSERT, second = prune
            raise database.sqlite3.OperationalError("simulated lock")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(database.sqlite3, "connect", failing_on_second_connect)
    # Must not raise even though the prune path fails.
    database.save_game_snapshot({"id": 3, "playing": 7, "visits": 70})
    # Verify with the real connector (the patched one raises on later calls).
    conn = real_connect(fresh_db)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM game_history")
    assert cur.fetchone()[0] == 1
    conn.close()
