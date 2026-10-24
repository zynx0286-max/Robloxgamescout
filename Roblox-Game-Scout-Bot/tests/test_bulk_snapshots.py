"""Tests for save_game_snapshots_bulk.

A scan cycle used to open one SQLite connection + commit per collected
game (~150 fsyncs/cycle). The bulk writer must land the exact same rows in
one transaction, prune on the same schedule, and be a no-op on an empty
list so callers need no special case.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_path = os.path.join(tempfile.mkdtemp(), "bulk-snap-test.db")

import database


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    database._last_history_prune = None
    yield _db_path


def _rows(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT game_id, players, visits FROM game_history ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return rows


def test_bulk_write_lands_every_row_in_one_call(fresh_db):
    games = [
        {"id": 1, "playing": 100, "visits": 1000},
        {"id": 2, "playing": 200, "visits": 2000},
        {"id": 3, "playing": 300, "visits": 3000},
    ]
    written = database.save_game_snapshots_bulk(games)
    assert written == 3
    assert set(_rows(fresh_db)) == {
        (1, 100, 1000), (2, 200, 2000), (3, 300, 3000)
    }


def test_bulk_write_skips_malformed_entries(fresh_db):
    games = [
        {"id": 7, "playing": 10, "visits": 100},
        {"id": 8},  # missing keys — skip, never raise
        "junk, not even a dict",
    ]
    written = database.save_game_snapshots_bulk(games)
    assert written == 1
    assert _rows(fresh_db) == [(7, 10, 100)]


def test_bulk_write_empty_list_is_a_noop(fresh_db):
    assert database.save_game_snapshots_bulk([]) == 0
    assert database.save_game_snapshots_bulk(None) == 0
    assert _rows(fresh_db) == []


def test_single_save_delegates_to_bulk(fresh_db, monkeypatch):
    calls = []
    real_bulk = database.save_game_snapshots_bulk

    def spy(games):
        calls.append(len(games or []))
        return real_bulk(games)

    monkeypatch.setattr(database, "save_game_snapshots_bulk", spy)
    database.save_game_snapshot({"id": 42, "playing": 5, "visits": 50})
    assert calls == [1]
    assert _rows(fresh_db) == [(42, 5, 50)]


def test_bulk_write_prunes_stale_rows_like_single_save(fresh_db):
    # A row far outside the 30-day retention window must go when a bulk
    # write triggers the prune window, exactly as the single-row path does.
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (99, 5, 50, datetime('now', '-60 days'))"
    )
    conn.commit()
    conn.close()

    database.save_game_snapshots_bulk([{"id": 1, "playing": 10, "visits": 100}])

    ids = {row[0] for row in _rows(fresh_db)}
    assert 99 not in ids
    assert 1 in ids


def test_scanner_uses_the_bulk_path():
    """Pipeline wiring: scan_games_async snapshots via the bulk writer.

    The old per-game call sat inside the enrichment loop; the bulk call
    must be made once, before the loop, exactly as implemented.
    """
    import scanner
    import inspect

    src = inspect.getsource(scanner.scan_games_async)
    assert "save_game_snapshots_bulk(enriched)" in src
    # The bulk call happens before the per-game processing loop, and the
    # per-game snapshot call inside that loop is gone.
    bulk_pos = src.index("save_game_snapshots_bulk(enriched)")
    loop_pos = src.index("for info in enriched:", bulk_pos)
    loop_body = src[loop_pos:]
    assert "save_game_snapshot(info)" not in loop_body
    assert src.index("save_game_snapshots_bulk(enriched)") < loop_pos
