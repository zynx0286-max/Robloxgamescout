"""Tests for update_watched_game_snapshots_bulk.

The tracker used to open one connection + commit per watched game per
cycle. The bulk writer must land the exact same rows in ONE transaction
and be a no-op on an empty batch so callers need no special case.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "bulk-watch-test.db")

import database


def _rows():
    conn = sqlite3.connect(_db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT game_id, last_players, last_visits FROM watched_games "
        "ORDER BY game_id"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _times():
    conn = sqlite3.connect(_db_path)
    cur = conn.cursor()
    cur.execute("SELECT game_id, last_checked FROM watched_games ORDER BY game_id")
    rows = cur.fetchall()
    conn.close()
    return dict(rows)


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    yield _db_path


def _watch(game_id, players=None, visits=None, user_id=0):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name, "
        "last_players, last_visits) VALUES (?, ?, ?, ?, ?)",
        (user_id, game_id, f"Game {game_id}", players, visits),
    )
    conn.commit()
    conn.close()


def test_bulk_update_lands_every_row_in_one_transaction(fresh_db):
    _watch(100, players=10, visits=100)
    _watch(200, players=20, visits=200)
    _watch(300, players=30, visits=300)

    conn = sqlite3.connect(_db_path)
    conn.execute("UPDATE watched_games SET last_checked = '1970-01-01 00:00:00'")
    conn.commit()
    conn.close()

    calls = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    original = database.sqlite3
    stub = type("M", (), {})()
    stub.connect = counting_connect
    stub.Error = original.Error
    stub.OperationalError = original.OperationalError
    database.sqlite3 = stub
    try:
        database.update_watched_game_snapshots_bulk(
            [(100, 555, 55), (200, 666, 66), (300, 777, 77)]
        )
    finally:
        database.sqlite3 = original

    assert len(calls) == 1  # ONE connection for the whole batch
    assert _rows() == [(100, 555, 55), (200, 666, 66), (300, 777, 77)]
    # CURRENT_TIMESTAMP stamped every touched row.
    assert all(t > "1970-01-01 00:00:00" for t in _times().values())


def test_bulk_update_is_a_noop_on_an_empty_batch(fresh_db):
    _watch(100, players=10, visits=100)
    before = _rows(), _times()

    database.update_watched_game_snapshots_bulk([])

    assert (_rows(), _times()) == before


def test_bulk_update_rejects_malformed_rows(fresh_db):
    with pytest.raises(ValueError):
        database.update_watched_game_snapshots_bulk([(100, 555)])


def test_bulk_update_only_touches_requested_games(fresh_db):
    _watch(100, players=10, visits=100)
    _watch(200, players=20, visits=200)

    database.update_watched_game_snapshots_bulk([(100, 999, 99)])

    assert _rows() == [(100, 999, 99), (200, 20, 200)]


def test_single_game_wrapper_still_works(fresh_db):
    """Back-compat: update_watched_game_snapshot remains available."""
    _watch(100, players=10, visits=100)
    database.update_watched_game_snapshot(100, 42, 4)
    assert _rows() == [(100, 42, 4)]
