"""Tests for the ascent radar's bulk name resolution + coverage diagnostics.

Two contracts:
* resolve_names must deliver the EXACT same name / was_alerted outcome per
  candidate as the per-game queries did, in ONE connection (was: 1-2
  queries per candidate on every minute tick);
* find_ascent_candidates must be able to report why in-window games did
  not fire, so a quiet radar can be told apart from a broken one.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ascent
import config
import database


def _db(tmp_path, monkeypatch) -> str:
    db_path = str(tmp_path / "ascent-bulk.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(ascent, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


def _seed(db_path, game_id, snapshots):
    conn = sqlite3.connect(db_path)
    for players, visits, offset in snapshots:
        conn.execute(
            "INSERT INTO game_history (game_id, players, visits, timestamp) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (game_id, players, visits, offset),
        )
    conn.commit()
    conn.close()


def _insert_alerted(db_path, game_id, name):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (?, ?)",
        (game_id, name),
    )
    conn.commit()
    conn.close()


def _insert_watched(db_path, game_id, name, user_id=0, date_added=None):
    conn = sqlite3.connect(db_path)
    ts = date_added or "2026-10-17 10:00:00"
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name, date_added) "
        "VALUES (?, ?, ?, ?)",
        (user_id, game_id, name, ts),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Bulk resolve_names
# ---------------------------------------------------------------------------


def test_bulk_resolve_delivers_the_same_contract(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _insert_alerted(db, 100, "Alerted Game")
    _insert_alerted(db, 101, None)  # alerted but unnamed -> watched fallback
    _insert_watched(db, 101, "Watched Fallback")
    # 102: two watched rows (distinct owners); the one with the newer
    # date_added must win — exactly what the old per-game query did.
    _insert_watched(db, 102, "Older Watched", user_id=1,
                    date_added="2026-10-17 09:00:00")
    _insert_watched(db, 102, "Newest Watched", user_id=2,
                    date_added="2026-10-17 12:00:00")
    _insert_watched(db, 103, "Never Alerted")
    # 104: known to nobody.

    candidates = [{"game_id": gid, "game_name": None, "was_alerted": None}
                  for gid in (100, 101, 102, 103, 104)]
    ascent.resolve_names(db, candidates)

    by_id = {c["game_id"]: c for c in candidates}
    assert by_id[100] == {"game_id": 100, "game_name": "Alerted Game",
                          "was_alerted": True}
    assert by_id[101] == {"game_id": 101, "game_name": "Watched Fallback",
                          "was_alerted": True}  # alerted row exists, name NULL
    assert by_id[102] == {"game_id": 102, "game_name": "Newest Watched",
                          "was_alerted": False}
    assert by_id[103] == {"game_id": 103, "game_name": "Never Alerted",
                          "was_alerted": False}
    assert by_id[104] == {"game_id": 104, "game_name": "Game 104",
                          "was_alerted": False}


def test_resolve_names_uses_one_connection_whole_batch(tmp_path, monkeypatch):
    """The bulk path: one connection for the whole candidate list."""
    db = _db(tmp_path, monkeypatch)
    for gid in range(20):
        _insert_alerted(db, gid, f"Game {gid}")
    candidates = [{"game_id": gid} for gid in range(20)]

    real_connect = sqlite3.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    import ascent as ascent_mod
    # ascend.py calls the sqlite3 module object it imported; patch it there.
    original_module = ascent_mod.sqlite3
    stub = type("M", (), {})()
    stub.connect = counting_connect
    stub.Error = original_module.Error
    ascent_mod.sqlite3 = stub
    try:
        ascent_mod.resolve_names(db, candidates)
    finally:
        ascent_mod.sqlite3 = original_module

    assert len(calls) == 1
    # And names still resolved for every candidate.
    assert all(c.get("game_name") == f"Game {c['game_id']}" for c in candidates)


def test_resolve_names_empty_list_is_a_noop(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    ascent.resolve_names(db, [])  # must not raise


# ---------------------------------------------------------------------------
# Coverage diagnostics
# ---------------------------------------------------------------------------


def test_reports_why_in_window_games_did_not_fire(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    # Fires: fresh spike above both bars.
    _seed(db, 100, [(30, 5000, "-50 minutes"),
                    (40, 5400, "-10 minutes"),
                    (111, 6000, "-2 minutes")])
    # Too few snapshots (2).
    _seed(db, 200, [(30, 5000, "-10 minutes"), (111, 6000, "-2 minutes")])
    # Newest below MIN_CCU (45 < 50).
    _seed(db, 201, [(30, 5000, "-50 minutes"),
                    (40, 5400, "-10 minutes"),
                    (45, 6000, "-2 minutes")])
    # Baseline all-zero pre-newest.
    _seed(db, 202, [(0, 5000, "-50 minutes"),
                    (0, 5400, "-10 minutes"),
                    (111, 6000, "-2 minutes")])
    # Below the ratio bar: spike 55/35 < 2.0.
    _seed(db, 203, [(30, 5000, "-50 minutes"),
                    (40, 5400, "-10 minutes"),
                    (55, 6000, "-2 minutes")])
    # Everything right but the newest reading is stale (>20 min old).
    _seed(db, 204, [(30, 5000, "-240 minutes"),
                    (40, 5400, "-180 minutes"),
                    (111, 6000, "-150 minutes")])

    candidates, coverage = ascent.find_ascent_candidates(
        db_path=db, return_diagnostics=True
    )

    assert [c["game_id"] for c in candidates] == [100]
    assert coverage["games_in_window"] == 6
    assert coverage["skipped_too_few_snapshots"] == 1
    assert coverage["skipped_below_min_ccu"] == 1
    assert coverage["skipped_baseline_zero"] == 1
    assert coverage["skipped_ratio_below_bar"] == 1
    assert coverage["skipped_stale_snapshot"] == 1
    # Totals must reconcile.
    assert (coverage["skipped_too_few_snapshots"]
            + coverage["skipped_below_min_ccu"]
            + coverage["skipped_baseline_zero"]
            + coverage["skipped_ratio_below_bar"]
            + coverage["skipped_stale_snapshot"]
            == coverage["games_in_window"] - len(candidates))


def test_empty_window_reports_zero_totals(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    candidates, coverage = ascent.find_ascent_candidates(
        db_path=db, return_diagnostics=True
    )
    assert candidates == []
    assert coverage["games_in_window"] == 0
    assert coverage["snapshots_in_window"] == 0


def test_default_call_still_returns_a_plain_list(tmp_path, monkeypatch):
    """Back-compat: without return_diagnostics the signature is unchanged."""
    db = _db(tmp_path, monkeypatch)
    result = ascent.find_ascent_candidates(db_path=db)
    assert isinstance(result, list)
