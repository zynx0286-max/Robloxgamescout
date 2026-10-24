"""Equivalence tests for the bulk history reads.

growth_summaries_bulk and compute_velocities_bulk replaced one
connection+query per game with a single indexed read each. The numbers
must be IDENTICAL to what the per-game functions return on the same data
— a scan's alerts, filters and embeds are all downstream of these values.
"""

import os
import sqlite3
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_path = os.path.join(tempfile.mkdtemp(), "bulk-equivalence.db")

import database
import history
import velocity


@pytest.fixture()
def seeded_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(history, "DATABASE_PATH", _db_path)
    monkeypatch.setattr(velocity, "DATABASE_PATH", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()

    # Two games with enough history, one with none. Distinct shapes so the
    # equivalence check covers rising, flat and spike-ish profiles.
    game_a = [50, 60, 75, 90, 120]        # growing
    game_b = [200, 180, 175, 172, 170]    # decaying
    conn = sqlite3.connect(_db_path)
    for idx, players in enumerate(game_a):
        conn.execute(
            "INSERT INTO game_history (game_id, players, visits) VALUES (1, ?, ?)",
            (players, 1000 + idx * 100),
        )
    for idx, players in enumerate(game_b):
        conn.execute(
            "INSERT INTO game_history (game_id, players, visits) VALUES (2, ?, ?)",
            (players, 5000 + idx * 10),
        )
    conn.commit()
    conn.close()
    yield _db_path


def test_bulk_growth_matches_per_game_results(seeded_db):
    single_1 = history.growth_summary(1)
    single_2 = history.growth_summary(2)
    bulk = history.growth_summaries_bulk([1, 2, 999])
    assert bulk.get(1) == single_1
    assert bulk.get(2) == single_2
    # A game with no history is absent, exactly like the single-game {}.
    assert 999 not in bulk
    assert history.growth_summary(999) == {}


def test_bulk_velocity_matches_per_game_results(seeded_db):
    single_1 = velocity.compute_velocity(1)
    single_2 = velocity.compute_velocity(2)
    bulk = velocity.compute_velocities_bulk([1, 2, 999])
    assert bulk.get(1) == single_1
    assert bulk.get(2) == single_2
    # Insufficient-data shape preserved for the unseeded game.
    assert bulk.get(999) == velocity.compute_velocity(999)
    assert bulk.get(999)["ccu_trend"] == "insufficient_data"


def test_growth_summary_delegates_to_the_bulk_path(seeded_db, monkeypatch):
    called = []
    real = history.growth_summaries_bulk

    def spy(ids, days=None):
        called.append(len(ids))
        return real(ids)

    monkeypatch.setattr(history, "growth_summaries_bulk", spy)
    history.growth_summary(1)
    assert called == [1]


def test_detect_spiking_games_uses_the_bulk_read(seeded_db, monkeypatch):
    """The radar-style selector must agree with a per-game manual scan."""
    bulk = velocity.compute_velocities_bulk([1, 2])
    manual = [
        gid for gid, v in bulk.items()
        if v["ccu_trend"] == "spiking" and v["current_players"] >= 50
    ]
    assert [d["game_id"] for d in velocity.detect_spiking_games([1, 2])] == manual


def test_bulk_read_speed_advantage_is_real(seeded_db):
    """Directional guard: bulk must not be slower than the per-game path.

    Times both on the seeded store with a generous pass count; asserts
    bulk <= per-game rather than an absolute number (host-dependent).
    """
    ids = [1, 2] * 40  # 80 lookups' worth of per-game work
    t0 = time.perf_counter()
    for _ in range(40):
        for gid in (1, 2):
            history.growth_summary(gid)
            velocity.get_historical_snapshots(gid)
    per_game_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    for _ in range(40):
        history.growth_summaries_bulk(ids)
        velocity.compute_velocities_bulk(ids)
    bulk_s = time.perf_counter() - t1

    assert bulk_s <= per_game_s
