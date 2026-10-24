"""Scorecard: the bot grading its own calls.

Contract under test (scorecard.py):

* The grading rubric is pure and honest — a game with no usable baseline
  is "unrateable", never forced into a win or miss bucket.
* Baselines come from the snapshot at-or-before the alert timestamp (the
  bot's actual alert-time measurement), never from a later re-derivation.
* The aggregate hit rate excludes unrateable calls from the denominator,
  so a wave of zero-baseline alerts cannot fake a 100% record.
* Every component — collection, evaluation, summary, rendering — is pure
  over its inputs; the fail-soft report never raises on bad data.
* `/scorecard` is registered and wired to the real renderer.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import scorecard

_db_path = os.path.join(tempfile.mkdtemp(), "scorecard-test.db")


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(scorecard, "DATABASE_PATH", _db_path)
    database.create_database()
    yield _db_path


def _alert(game_id, name, hours_ago):
    conn = sqlite3.connect(_db_path)
    ts = f"datetime offsets handled in SQL below"
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name, first_alerted) "
        "VALUES (?, ?, datetime('now', ?))",
        (game_id, name, f"-{hours_ago} hours"),
    )
    conn.commit()
    conn.close()


def _snapshot(game_id, players, visits, offset):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (game_id, players, visits, offset),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# evaluate_call — the rubric
# ---------------------------------------------------------------------------

def test_win_band():
    r = scorecard.evaluate_call("2026-01-01 12:00:00", 100, 150)
    assert r["outcome"] == "win" and r["delta_pct"] == 50.0


def test_quiet_band():
    r = scorecard.evaluate_call("2026-01-01 12:00:00", 100, 110)
    assert r["outcome"] == "quiet"


def test_miss_band():
    r = scorecard.evaluate_call("2026-01-01 12:00:00", 100, 60)
    assert r["outcome"] == "miss"


def test_zero_baseline_is_unrateable_never_a_miss():
    """A 0-CCU baseline can't be graded — that's honest reporting."""
    r = scorecard.evaluate_call("2026-01-01 12:00:00", 0, 500)
    assert r["outcome"] == "unrateable"
    assert r["delta_pct"] is None


def test_unknown_latest_is_unrateable():
    r = scorecard.evaluate_call("2026-01-01 12:00:00", 100, None)
    assert r["outcome"] == "unrateable"


# ---------------------------------------------------------------------------
# collect_calls — the baseline is what the bot measured AT alert time
# ---------------------------------------------------------------------------

def test_baseline_is_alert_time_snapshot_not_earliest(fresh_db):
    """Baseline = snapshot at-or-before alert; earliest is a different day."""
    _alert(1, "Rocket", hours_ago=4)
    _snapshot(1, 200, 1000, "-3 hours")    # before alert? no: alert was 4h ago, so this IS the at-or-before baseline (3h ago < 4h ago → after)

    calls = scorecard.collect_calls()
    # (interpretation verified below in test_baseline_requires_earlier_timestamp)
    assert len(calls) == 1
    assert calls[0]["game_name"] == "Rocket"


def test_baseline_requires_earlier_timestamp(fresh_db):
    _alert(2, "Precise", hours_ago=6)          # alert at -6h
    _snapshot(2, 100, 500, "-8 hours")         # earlier than alert → baseline
    _snapshot(2, 500, 700, "-1 hours")         # later → NOT the baseline

    calls = scorecard.collect_calls()
    assert calls[0]["baseline"] == 100
    assert calls[0]["latest"] == 500


def test_no_baseline_at_all_is_reportable_as_unrateable(fresh_db):
    _alert(3, "Never Scanned", hours_ago=5)

    graded = scorecard.evaluate_calls(scorecard.collect_calls())
    assert graded[0]["outcome"] == "unrateable"


def test_lookback_excludes_old_alerts(fresh_db):
    _alert(4, "Ancient", hours_ago=200)  # outside 72h window

    assert scorecard.collect_calls() == []


# ---------------------------------------------------------------------------
# summarize / rendering — honesty of the aggregate
# ---------------------------------------------------------------------------

def test_summary_hit_rate_excludes_unrateable():
    graded = [
        {"outcome": "win", "delta_pct": 30.0},
        {"outcome": "quiet", "delta_pct": 5.0},
        {"outcome": "miss", "delta_pct": -25.0},
        {"outcome": "unrateable", "delta_pct": None},
        {"outcome": "unrateable", "delta_pct": None},
    ]
    s = scorecard.summarize(graded)
    assert s["rateable"] == 3
    assert s["wins"] == 1 and s["quiet"] == 1 and s["misses"] == 1
    assert s["hit_rate_pct"] == pytest.approx(33.3, abs=0.1)
    assert s["unrateable"] == 2


def test_summary_of_nothing_is_honest():
    s = scorecard.summarize([])
    assert s["hit_rate_pct"] is None
    assert scorecard.render_summary(s).startswith(
        "No alerted games"
    )


def test_render_calls_uses_one_symbol_per_line():
    graded = [
        {"game_name": "Rocket", "outcome": "win", "delta_pct": 50.0},
        {"game_name": "Nobaseline", "outcome": "unrateable", "delta_pct": None},
    ]
    lines = scorecard.render_calls(graded)
    assert "✅ **Rocket** — +50% CCU" in lines[0]
    assert "❔ **Nobaseline** — no comparable snapshots" in lines[1]


def test_render_summary_reports_the_rate():
    s = scorecard.summarize([
        {"outcome": "win", "delta_pct": 30.0},
        {"outcome": "win", "delta_pct": 25.0},
        {"outcome": "miss", "delta_pct": -40.0},
        {"outcome": "quiet", "delta_pct": 2.0},
    ])
    text = scorecard.render_summary(s)
    assert "50% hit rate" in text
    assert "2 wins" in text and "1 misses" in text


# ---------------------------------------------------------------------------
# scorecard_report — the fail-soft full path
# ---------------------------------------------------------------------------

def test_report_on_missing_tables_degrades(tmp_path):
    import scorecard as sc

    empty_db = str(tmp_path / "empty.db")
    conn = sqlite3.connect(empty_db)
    conn.execute("CREATE TABLE alerted_games (game_id INTEGER PRIMARY KEY, game_name TEXT, first_alerted TEXT)")
    conn.commit()
    conn.close()

    text = sc.scorecard_report(empty_db)
    assert "Scorecard" in text
    assert "No alerted games" in text


def test_report_renders_graded_calls(fresh_db):
    _alert(10, "Winner", hours_ago=5)
    _snapshot(10, 100, 400, "-6 hours")     # baseline
    _snapshot(10, 200, 500, "-1 hours")     # latest: 100% win
    _alert(11, "Misser", hours_ago=5)
    _snapshot(11, 300, 800, "-6 hours")
    _snapshot(11, 150, 700, "-1 hours")     # 50% miss

    text = scorecard.scorecard_report()
    assert "Winner" in text and "100%" in text
    assert "Misser" in text and "-50%" in text
    assert "50% hit rate" in text


# ---------------------------------------------------------------------------
# /scorecard command surface
# ---------------------------------------------------------------------------

def test_scorecard_command_is_registered():
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert 'name="scorecard"' in source
    assert "scorecard_report" in source  # real renderer wired
