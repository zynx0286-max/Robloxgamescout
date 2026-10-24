"""Tests for the /radar diagnostics page (renderer + read path).

The renderer is pure (data in → text out); the read path is the same
data the minute tick uses (find_ascent_candidates + resolve_names), so
/radar can never disagree with what the radar actually saw.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ascent
import config
import database


def _db(tmp_path, monkeypatch) -> str:
    db_path = str(tmp_path / "radar-status.db")
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


# ---------------------------------------------------------------------------
# Pure renderer
# ---------------------------------------------------------------------------

_COVERAGE = {
    "games_in_window": 6,
    "snapshots_in_window": 18,
    "skipped_too_few_snapshots": 1,
    "skipped_below_min_ccu": 1,
    "skipped_baseline_zero": 1,
    "skipped_ratio_below_bar": 1,
    "skipped_stale_snapshot": 1,
}

_CANDIDATE = {
    "game_id": 100,
    "game_name": "Spike Game",
    "latest_players": 111,
    "baseline_players": 35.0,
    "spike_ratio": 3.17,
    "rise_ratio": 3.7,
    "n_snapshots": 3,
    "was_alerted": True,
    "age_minutes": 2.0,
}


def test_renderer_names_coverage_and_candidates():
    text = ascent.render_radar_status([dict(_CANDIDATE)], _COVERAGE)

    assert "🛰 **Ascent radar" in text
    # Every skip reason is named with its count.
    assert "short-history=1" in text
    assert "ccu=1" in text
    assert "zero-base=1" in text
    assert "below-bar=1" in text
    assert "stale=1" in text
    assert "Spike Game" in text
    assert "3.17× avg" in text
    # An already-alerted game renders as an escalation, not a first fire.
    assert "🔥 escalate" in text


def test_renderer_quiet_radar_reports_no_candidates():
    text = ascent.render_radar_status([], _COVERAGE)
    assert "_Radar quiet" in text
    assert "6** games" in text


def test_renderer_escapes_to_game_id_when_unresolved():
    c = dict(_CANDIDATE, game_name=None)
    text = ascent.render_radar_status([c], _COVERAGE)
    assert "Game 100" in text


def test_renderer_caps_candidate_lines():
    candidates = [dict(_CANDIDATE, game_id=i, game_name=f"G{i}")
                  for i in range(12)]
    text = ascent.render_radar_status(candidates, _COVERAGE, max_lines=3)
    assert "…and 9 more." in text


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def test_radar_status_uses_the_ticker_pipeline(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 100, [(30, 5000, "-50 minutes"),
                    (40, 5400, "-10 minutes"),
                    (111, 6000, "-2 minutes")])
    database.mark_alerted(100, "Spike Game")

    text = ascent.radar_status(db_path=db)

    assert "Spike Game" in text          # resolved via the alerted ledger
    assert "🔥 escalate" in text
    assert "1 candidate(s)" in text


def test_radar_status_degrades_on_a_broken_store(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    # Corrupt the table the radar reads.
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE game_history")
    conn.commit()
    conn.close()

    text = ascent.radar_status(db_path=db)  # must not raise

    assert "unavailable" in text


def test_radar_status_empty_store_is_quiet(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    text = ascent.radar_status(db_path=db)
    assert "_Radar quiet" in text
