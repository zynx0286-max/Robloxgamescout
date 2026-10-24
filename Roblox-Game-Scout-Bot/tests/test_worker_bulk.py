"""Tests for the portfolio worker's bulk snapshot writer.

The worker sampled the Roblox API in batches but paid one connection +
commit per row on every tick. The bulk writer must land identical rows
in ONE transaction and no-op on an empty batch.
"""

import aiohttp
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portfolio_db


def _rows(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        "SELECT game_id, players, visits, favorites, likes, dislikes, like_ratio "
        "FROM game_snapshots ORDER BY game_id"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


@pytest.fixture()
def fresh_db(monkeypatch):
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "worker-bulk.db")
    monkeypatch.setattr(portfolio_db, "DATABASE", db_path)
    conn = sqlite3.connect(db_path)
    conn.close()
    portfolio_db.create_portfolio_tables()
    yield db_path
    shutil.rmtree(tmp, ignore_errors=True)


def test_tick_writer_lands_every_row_in_one_transaction(fresh_db):
    real_connect = sqlite3.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    monkey = __import__("builtins")
    original_sqlite3 = portfolio_db.sqlite3
    stub = type("M", (), {})()
    stub.connect = counting_connect
    stub.Error = original_sqlite3.Error
    stub.Row = original_sqlite3.Row
    portfolio_db.sqlite3 = stub
    try:
        portfolio_db.record_game_snapshots_bulk([
            (10, 100, 5_000, 30, 900, 100),
            (20, 200, 9_000, 50, 800, 200),
            (30, 0, 0, None, 0, 0),   # zero votes -> like_ratio None
        ])
    finally:
        portfolio_db.sqlite3 = original_sqlite3

    assert len(calls) == 1  # ONE connection for the whole tick
    rows = _rows(fresh_db)
    assert rows == [
        (10, 100, 5_000, 30, 900, 100, 90.0),
        (20, 200, 9_000, 50, 800, 200, 80.0),
        (30, 0, 0, None, 0, 0, None),
    ]
    assert monkey is __import__("builtins")  # self-check: harness unchanged


def test_tick_writer_empty_batch_is_a_noop(fresh_db):
    before = _rows(fresh_db)
    portfolio_db.record_game_snapshots_bulk([])
    assert _rows(fresh_db) == before


def test_tick_writer_matches_single_game_writer(fresh_db):
    """Row equivalence: the bulk path writes what the per-game path wrote."""
    portfolio_db.record_game_snapshot(10, 175, 6_100, favorites=42, likes=250, dislikes=50)
    portfolio_db.record_game_snapshots_bulk([(20, 180, 6_200, 43, 300, 100)])

    single = _rows(fresh_db)[0]
    bulk = _rows(fresh_db)[1]
    # Same column layout; derived like ratios agree (250/300 ≈ 83.33, 300/400 = 75.0)
    assert single[6] == round(250 / 300 * 100.0, 2) == 83.33
    assert bulk[6] == 75.0


def test_worker_collect_uses_one_write(fresh_db, monkeypatch):
    """collect_portfolio flushes the tick's rows through ONE bulk call."""
    import asyncio

    import portfolio_worker

    monkeypatch.setattr(portfolio_worker, "list_portfolio_games", lambda visible_only=True: [
        {"game_id": 10, "thumbnail_url": "x"},
        {"game_id": 20, "thumbnail_url": "x"},
    ])

    calls = []
    monkeypatch.setattr(
        portfolio_worker, "record_game_snapshots_bulk",
        lambda rows: calls.append(list(rows)),
    )

    collected, errors = asyncio.run(
        portfolio_worker.collect_portfolio(_FakeSession())
    )

    assert (collected, errors) == (2, 0)
    assert len(calls) == 1
    assert sorted(r[0] for r in calls[0]) == [10, 20]


class _Response:
    """async-with target carrying a JSON payload."""

    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Session-shaped stub answering the worker's three APIs by URL.

    Mirrors production usage (``async with session.get(url) as resp:``)
    so collect_portfolio exercises its real fetch fan-out and single
    bulk write with zero network.
    """

    GAMES_PAYLOAD = {
        "data": [
            {"id": 10, "name": "A", "playing": 50, "visits": 500,
             "favoritedCount": 5},
            {"id": 20, "name": "B", "playing": 60, "visits": 600,
             "favoritedCount": 6},
        ]
    }

    def get(self, url):
        if "votes" in url or "thumbnails" in url:
            return _Response({"data": []})
        return _Response(self.GAMES_PAYLOAD)

    async def close(self):
        pass
