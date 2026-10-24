"""Tests for chart_rank.attach_rank_signals_bulk.

Two indexed queries for the whole scan cycle replace one connection + two
queries per charted game. The fields attached must be IDENTICAL to what
the per-game attach_rank_signal sets on the same store.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_path = os.path.join(tempfile.mkdtemp(), "bulk-rank.db")

import database
import chart_rank


def _seed(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    monkeypatch_free_conn = sqlite3.connect(db_path)
    monkeypatch_free_conn.close()
    database.create_database()

    # Three prior cycles for game 1 (rising), one prior for game 2
    # (falling), none for game 3 (not charted before). Game 4 never charted.
    cycles = [
        (1, "trending", "Top Trending", 20, "2026-01-01 00:00:00.000000"),
        (1, "trending", "Top Trending", 10, "2026-01-02 00:00:00.000000"),
        (1, "trending", "Top Trending", 5,  "2026-01-03 00:00:00.000000"),
        (2, "trending", "Top Trending", 3,  "2026-01-02 00:00:00.000000"),
    ]
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO chart_positions (game_id, sort_id, sort_label, position, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        cycles,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def seeded(monkeypatch):
    monkeypatch.setattr(database, "DATABASE", _db_path)
    _seed(_db_path)
    chart_rank._current_cycle_ts = "2026-01-04 00:00:00.000000"
    yield
    chart_rank._current_cycle_ts = None


def _game(gid, rank=8, sort_id="trending", sort_label="Top Trending"):
    return {"id": gid, "chart_rank": rank, "charts_sort_id": sort_id,
            "charts_sort": sort_label}


def test_bulk_result_matches_per_game_signal(seeded):
    game_a, game_b = _game(1), _game(2)

    # Reference: the per-game signal on the SAME seeded store.
    ref_a = _game(1)
    chart_rank.attach_rank_signal(ref_a)
    ref_b = _game(2)
    chart_rank.attach_rank_signal(ref_b)

    # Diagnostic ordering note: attach_rank_signal's writes move
    # _current_cycle_ts only in record_positions, so the two calls above do
    # not perturb the bulk pass below.
    chart_rank.attach_rank_signals_bulk([game_a, game_b])

    for key in ("chart_rank", "chart_rank_delta", "chart_best"):
        assert game_a.get(key) == ref_a.get(key), key
        assert game_b.get(key) == ref_b.get(key), key


def test_values_are_correct_not_just_equal(seeded):
    game = _game(1)  # rank 8 now; prior best 5; best ever 5
    chart_rank.attach_rank_signals_bulk([game])
    assert game["chart_rank"] == 8
    assert game["chart_rank_delta"] == -3      # fell 5 -> 8 since last cycle
    assert game["chart_best"] == 5


def test_uncharted_game_is_left_untouched(seeded):
    game = {"id": 4}  # no appearances, no stub rank
    before = dict(game)
    chart_rank.attach_rank_signals_bulk([game])
    assert game == before


def test_never_charted_game_with_stub_gets_no_delta_no_best(seeded):
    game = _game(3)  # no prior rows at all, and no rows this cycle either
    chart_rank.attach_rank_signals_bulk([game])
    # The store has nothing on this game, so neither delta nor best can be
    # derived — identical to what the per-game path leaves unset.
    assert game["chart_rank"] == 8
    assert "chart_rank_delta" not in game
    assert "chart_best" not in game


def test_empty_input_is_a_noop(seeded):
    assert chart_rank.attach_rank_signals_bulk(None) == 0
    assert chart_rank.attach_rank_signals_bulk([]) == 0


def test_returns_count_of_attached_games(seeded):
    games = [_game(1), _game(2), {"id": 4}, _game(3)]
    attached = chart_rank.attach_rank_signals_bulk(games)
    assert attached == 3
