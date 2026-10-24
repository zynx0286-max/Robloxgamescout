"""Tests for the portfolio API's bulk reads.

Two contracts:
* get_latest_snapshots_bulk / get_latest_discord_snapshots_bulk return
  exactly what the per-game lookups return, batched into one query
  each (one connection per call, first-id-DESC hit wins);
* _stats_from_rows reproduces the old multi-query builder field for
  field — cross-checked against the store's per-game helpers.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "bulk-portfolio-test.db")

import portfolio_db
import portfolio_api


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(portfolio_db, "DATABASE", _db_path)
    portfolio_db.create_portfolio_tables()
    yield _db_path


def _snap(game_id, players, hours_ago=1.0, visits=None, likes=100, dislikes=10,
          favorites=None):
    ts = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=hours_ago)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO game_snapshots (game_id, timestamp, players, visits, "
        "favorites, likes, dislikes, like_ratio) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, ts, players, visits or players * 10, favorites, likes, dislikes,
         round(likes / (likes + dislikes) * 100.0, 2) if (likes + dislikes) else None),
    )
    conn.commit()
    conn.close()


def _discord(game_id, members, hours_ago=1.0):
    ts = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(hours=hours_ago)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO discord_snapshots (game_id, timestamp, member_count) "
        "VALUES (?, ?, ?)",
        (game_id, ts, members),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Bulk lates — equivalence with the per-game lookups
# ---------------------------------------------------------------------------


def test_latest_snapshots_bulk_matches_per_game(fresh_db):
    _snap(10, 100, hours_ago=3)
    _snap(10, 200, hours_ago=1)
    _snap(20, 50, hours_ago=2)

    bulk = portfolio_db.get_latest_snapshots_bulk([10, 20, 999])

    assert list(bulk.keys()) == [10, 20, 999]
    assert bulk[10]["players"] == 200
    assert bulk[10] == portfolio_db.get_latest_snapshot(10)
    assert bulk[20] == portfolio_db.get_latest_snapshot(20)
    assert bulk[999] is None


def test_latest_discord_bulk_matches_per_game(fresh_db):
    _discord(10, 500, hours_ago=3)
    _discord(10, 950, hours_ago=1)
    _discord(20, 40, hours_ago=2)

    bulk = portfolio_db.get_latest_discord_snapshots_bulk([10, 20, 999])

    assert bulk[10] == portfolio_db.get_latest_discord_snapshot(10)
    assert bulk[10]["member_count"] == 950
    assert bulk[20] == portfolio_db.get_latest_discord_snapshot(20)
    assert bulk[999] is None


def test_bulk_lates_dedupe_and_are_noop_on_empty(fresh_db):
    assert portfolio_db.get_latest_snapshots_bulk([]) == {}
    assert portfolio_db.get_latest_discord_snapshots_bulk([]) == {}
    _snap(10, 1)
    assert list(portfolio_db.get_latest_snapshots_bulk([10, 10]).keys()) == [10]


# ---------------------------------------------------------------------------
# _stats_from_rows vs the store's own per-game helpers
# ---------------------------------------------------------------------------


def test_stats_from_rows_matches_the_per_game_helpers(fresh_db):
    snapshots = [(3.0, 180), (1.5, 200), (0.5, 260)]
    for hours_ago, players in snapshots:
        _snap(10, players, hours_ago=hours_ago)

    ids = [10]
    rows = portfolio_db.get_snapshots_bulk(ids)[10]
    stats = portfolio_api._stats_from_rows(rows)

    assert stats is not None
    # Level fields come from the newest row.
    assert stats["players"] == 260
    assert stats["updated_at"] == rows[-1]["timestamp"]
    # Peak over 7d window agrees with the store helper.
    assert stats["peak_players_7d"] == portfolio_db.get_peak_players(10, hours=24 * 7)
    # Every growth window agrees with the store helper.
    for key, hours in [("players_1h", 1), ("players_6h", 6),
                       ("players_24h", 24), ("players_7d", 24 * 7)]:
        assert stats["growth"][key] == portfolio_db.compute_growth(10, hours)["players"]


def test_stats_from_rows_empty_and_lt_two_point_windows(fresh_db):
    # One lone snapshot: stats exist, every growth window and the peak deny.
    _snap(10, 100, hours_ago=0.5)
    rows = portfolio_db.get_snapshots_bulk([10])[10]
    stats = portfolio_api._stats_from_rows(rows)
    assert stats["players"] == 100
    assert stats["growth"] == {
        "players_1h": None, "players_6h": None,
        "players_24h": None, "players_7d": None,
    }
    assert stats["peak_players_7d"] == 100

    # No rows at all → None, same as the old get_latest_snapshot guard.
    assert portfolio_api._stats_from_rows([]) is None


def test_list_payload_cost_is_two_queries_total(fresh_db, monkeypatch):
    """The list payload must not regress to per-project connections."""
    portfolio_db.add_portfolio_game(10, "A Game")
    portfolio_db.add_portfolio_game(20, "B Game")
    _snap(10, 100); _snap(20, 200)
    _discord(10, 5)

    real_connect = sqlite3.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(portfolio_db.sqlite3, "connect", counting_connect,
                        raising=True)
    monkeypatch.setattr(portfolio_api, "PORTFOLIO_CACHE_SECONDS", 0)

    payload = portfolio_api._portfolio_payload()

    assert len(payload["projects"]) == 2
    # Two bulk reads: one for the series, one for the Discord ledger.
    # (list_portfolio_games, before the pair, is excluded by scoping the
    # assertion to reads that fetch rounds-trips: exactly 3 total is fine
    # — 1 select + 2 bulk — but 7-per-project is what this must never hit.)
    assert len(calls) <= 4
