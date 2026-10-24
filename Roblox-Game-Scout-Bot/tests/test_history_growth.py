"""Native growth history, computed from the bot's own snapshots.

This replaced a third-party metric API, so the contract matters: "not enough
history yet" must never be reported as 0% growth, because a new game would
then look flat rather than unknown.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import history


def _db(tmp_path, monkeypatch) -> str:
    """Point every module that owns DATABASE_PATH at a throwaway database."""
    import config
    import database

    db_path = str(tmp_path / "history.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(history, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


def _snapshot(db_path, game_id, players, visits, offset):
    """Insert a snapshot at NOW + offset (e.g. '-6 days')."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (game_id, players, visits, offset),
    )
    conn.commit()
    conn.close()


def test_growth_needs_two_snapshots(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 1, 100, 10_000, "-1 days")
    assert history.growth_summary(1) == {}


def test_growth_with_no_snapshots_at_all(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert history.growth_summary(999) == {}


def test_growth_summary_computes_ccu_and_visit_change(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 2, 100, 10_000, "-6 days")
    _snapshot(db, 2, 150, 12_000, "-1 hours")

    summary = history.growth_summary(2)

    assert summary["ccu_growth_pct"] == 50.0
    assert summary["visits_growth_pct"] == 20.0
    assert summary["snapshot_count"] == 2
    assert summary["history_days"] == 7


def test_snapshots_outside_the_window_are_ignored(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 3, 10, 1_000, "-40 days")
    _snapshot(db, 3, 100, 10_000, "-1 days")

    # The 40-day-old row is outside the default 7-day window, so only one
    # usable snapshot remains and the summary stays empty.
    assert history.growth_summary(3) == {}


def test_a_wider_window_picks_up_older_history(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 4, 50, 5_000, "-20 days")
    _snapshot(db, 4, 100, 10_000, "-1 days")

    summary = history.growth_summary(4, days=30)

    assert summary["ccu_growth_pct"] == 100.0
    assert summary["history_days"] == 30


def test_zero_baseline_reports_unknown_not_zero(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 5, 0, 0, "-6 days")
    _snapshot(db, 5, 40, 400, "-1 hours")

    summary = history.growth_summary(5)

    assert summary["ccu_growth_pct"] is None
    assert summary["visits_growth_pct"] is None
    # The summary itself is still returned — the caller can still show
    # "tracking since" information even when a ratio is impossible.
    assert summary["snapshot_count"] == 2


def test_growth_handles_a_decline(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _snapshot(db, 6, 200, 20_000, "-6 days")
    _snapshot(db, 6, 100, 10_000, "-1 hours")

    summary = history.growth_summary(6)

    assert summary["ccu_growth_pct"] == -50.0
    assert summary["visits_growth_pct"] == -50.0


def test_non_numeric_game_id_is_ignored(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert history.growth_summary(None) == {}
    assert history.get_window_snapshots("not-an-id") == []


def test_count_snapshots(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    assert history.count_snapshots() == 0

    _snapshot(db, 7, 1, 1, "-1 days")
    _snapshot(db, 8, 1, 1, "-1 days")

    assert history.count_snapshots() == 2


def test_compute_growth_pct_edge_cases():
    assert history.compute_growth_pct(100, 150) == 50.0
    assert history.compute_growth_pct(0, 150) is None
    assert history.compute_growth_pct(None, 150) is None
    assert history.compute_growth_pct(100, None) is None
    assert history.compute_growth_pct("nonsense", 5) is None
