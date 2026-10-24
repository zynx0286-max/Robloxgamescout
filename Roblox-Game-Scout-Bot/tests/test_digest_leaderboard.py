"""Tests for the morning digest's Scout leaderboard section.

Contract: the digest surfaces the all-time /leaderboard ranking (same
store, same top-alerted-by-scout-score selector), renders nothing on an
empty store, and a store failure degrades to a note instead of eating
the digest — exactly the pulse section's fail-soft behavior.
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "digest-lb-test.db")

import database
import scheduler

ET = ZoneInfo("America/New_York")

LEADERBOARD_HEADING = "🏁 **Scout leaderboard — all time**"


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    database.create_database()
    yield _db_path


def _digest(rows=None, alerts_count=0, cycles_count=0):
    return scheduler.build_digest_message(
        alerted_rows=rows or [],
        alerts_count=alerts_count,
        cycles_count=cycles_count,
        now=datetime(2026, 10, 17, 10, 0, tzinfo=ET),
    )


# ---------------------------------------------------------------------------
# Pure rendering (_render_leaderboard_lines)
# ---------------------------------------------------------------------------


def test_render_medals_scores_and_repeat_counts():
    rows = [
        (10, "Gold Game", 95, 3, "2026-10-01 10:00:00"),
        (20, "Silver Game", 70, 1, "2026-10-02 10:00:00"),
        (30, None, None, 1, "2026-10-03 10:00:00"),  # unnamed + unscored
    ]
    lines = scheduler._render_leaderboard_lines(rows)
    assert lines[0] == "🥇 Gold Game — `95/100` · seen 3"
    assert lines[1] == "🥈 Silver Game — `70/100`"
    assert lines[2] == "🥉 ID 30 — `—`"
    # Single-seen games carry no repeat suffix.
    assert "· seen" not in lines[1]


def test_render_empty_store_renders_nothing():
    assert scheduler._render_leaderboard_lines([]) == []


# ---------------------------------------------------------------------------
# Digest integration (lazy store read)
# ---------------------------------------------------------------------------


def test_digest_includes_leaderboard_when_the_store_has_alerts(fresh_db):
    database.mark_alerted(10, "Gold Game", scout_score=95)
    database.mark_alerted(20, "Silver Game", scout_score=70)

    text = _digest()

    assert LEADERBOARD_HEADING in text
    assert "🥇 Gold Game — `95/100`" in text
    assert "🥈 Silver Game — `70/100`" in text


def test_digest_has_no_leaderboard_section_on_an_empty_store(fresh_db):
    text = _digest()
    assert LEADERBOARD_HEADING not in text


def test_leaderboard_failure_degrades_without_breaking_the_digest(
    fresh_db, monkeypatch
):
    def broken(limit=10, days=30):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(database, "top_alerted_by_scout_score", broken)

    text = _digest()  # must not raise

    assert "Good morning" in text
    assert "_Leaderboard section unavailable:" in text


def test_injected_rows_skip_the_store(fresh_db):
    """leaderboard_rows must be honoured verbatim (pure-function path)."""
    rows = [(10, "Injected Game", 88, 1, "2026-10-01 10:00:00")]

    text = scheduler.build_digest_message(
        alerted_rows=[],
        alerts_count=0,
        cycles_count=0,
        now=datetime(2026, 10, 17, 10, 0, tzinfo=ET),
        leaderboard_rows=rows,
    )

    assert LEADERBOARD_HEADING in text
    assert "Injected Game" in text
