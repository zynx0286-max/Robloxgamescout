"""Dossier: everything the bot knows about one game, on demand.

Contract under test (dossier.py):

* The narrative (`pure_llm_style_report`) is PURE and deterministic —
  the verdict comes from a fixed rubric over the measured signals, never
  from a network call, and identical inputs always produce identical
  output.
* The scoreboard (`compose_scoreboard`) renders a fixed-order section
  list where every section degrades to a quiet line, never a blank
  section or a crash.
* `gather_ledger` reads only the bot's own tables and handles the unseen
  game gracefully (zeros and Nones, not KeyError.
* `build_dossier_view` makes exactly ONE Roblox request and still
  produces a complete local report when it fails — with an explicit
  stale-state note instead of an error message.
* `/dossier` is registered and wired to the real renderer.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import dossier

_db_path = os.path.join(tempfile.mkdtemp(), "dossier-test.db")


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(dossier, "DATABASE_PATH", _db_path)
    # history and velocity each hold their own config-derived path binding —
    # every module that owns the database path must be patched or the
    # selector silently reads an empty default db instead of the fixture.
    import history
    import velocity
    monkeypatch.setattr(history, "DATABASE_PATH", _db_path)
    monkeypatch.setattr(velocity, "DATABASE_PATH", _db_path)
    database.create_database()
    yield _db_path


# ---------------------------------------------------------------------------
# pure_llm_style_report — the deterministic narrative
# ---------------------------------------------------------------------------

def test_narrative_is_deterministic():
    state = {
        "name": "Rocket",
        "growth": {"ccu_growth_pct": 55.0, "visits_growth_pct": 20.0,
                   "history_days": 7, "snapshot_count": 4},
        "velocity": {"ccu_spike": 2.4, "ccu_trend": "spiking",
                     "snapshot_count": 4},
        "chart_positions": [("Up & Coming", 8)],
        "ledger": {"first_alerted": "2026-01-01 12:00:00", "watchers": 2},
        "verdict": {"verdict": "Looks like a breakout", "confidence": 87},
    }
    a = dossier.pure_llm_style_report(state)
    b = dossier.pure_llm_style_report(state)
    assert a == b  # pure and deterministic


def test_narrative_strong_momentum_band():
    state = {
        "name": "Rocket",
        "growth": {"ccu_growth_pct": 60.0},   # 2 pts
        "velocity": {"ccu_spike": 2.2},       # 2 pts
        "chart_positions": [("Top", 3)],      # 1 pt
        "ledger": {"first_alerted": "x"},     # 1 pt
    }
    text = dossier.pure_llm_style_report(state)
    assert "Strong momentum" in text
    assert "Because:" in text
    assert "SP" in text or "momentum 2.2" in text  # measured in the reasoning


def test_narrative_flat_band_with_no_signal():
    state = {"name": "Quiet", "growth": {}, "velocity": {},
             "chart_positions": [], "ledger": {}}
    text = dossier.pure_llm_style_report(state)
    assert "Flat" in text
    assert "no qualifying signals" in text


def test_narrative_growing_band():
    state = {
        "name": "Climber",
        "growth": {"ccu_growth_pct": 20.0},   # 1 pt
        "velocity": {"ccu_spike": 1.4},       # 1 pt
        "chart_positions": [],
        "ledger": {"watchers": 1},            # 1 pt
    }
    text = dossier.pure_llm_style_report(state)
    assert "Building" in text


def test_narrative_includes_stored_ai_verdict_when_present():
    state = {
        "name": "Both",
        "growth": {}, "velocity": {}, "chart_positions": [], "ledger": {},
        "verdict": {"verdict": "Hold and watch", "confidence": 55},
    }
    text = dossier.pure_llm_style_report(state)
    assert "Hold and watch" in text
    assert "Stored AI take" in text


# ---------------------------------------------------------------------------
# compose_scoreboard — fixed-order, graceful sections
# ---------------------------------------------------------------------------

def test_scoreboard_renders_all_sections_for_full_state():
    state = {
        "name": "Rocket",
        "playing": 1200,
        "visits": 3400000,
        "favorites": 88000,
        "created_at": "2025-07-01T00:00:00Z",
        "state_stale": False,
        "growth": {"ccu_growth_pct": 42.0, "visits_growth_pct": 5.0,
                   "history_days": 7, "snapshot_count": 6},
        "velocity": {"ccu_spike": 1.8, "ccu_trend": "growing",
                     "snapshot_count": 6},
        "chart_positions": [("Top Trending", 12)],
        "chart_text": "#12 on Top Trending",
        "ledger": {"first_alerted": "2026-01-03 09:00:00",
                   "watchers": 3,
                   "watched_since": "2026-01-03 09:00:00",
                   "ascent_first": "2026-01-04 14:00:00",
                   "ascent_last": "2026-01-04 14:30:00",
                   "ascent_kind": "first"},
        "verdict": {"verdict": "Promising", "confidence": 72},
        "roots": {"lines": ["👨‍💻 Acme Studio",
                             "Group with 12,000 members"]},
    }
    text = dossier.compose_scoreboard(state)
    assert "Dossier — Rocket" in text
    assert "1,200" in text  # live state formatted
    assert "Window growth" in text
    assert "Momentum" in text
    assert "Top Trending #12" in text
    assert "first alerted 2026-01-03" in text
    assert "Promising" in text
    assert "Acme Studio" in text
    assert "Group with 12,000 members" in text


def test_scoreboard_flags_when_live_fetch_failed():
    state = {
        "name": "Rocket", "playing": None, "visits": None,
        "favorites": None, "created_at": None,
        "state_stale": True,
        "growth": {}, "velocity": {},
        "chart_positions": [], "chart_text": None,
        "ledger": {}, "verdict": None, "roots": None,
    }
    text = dossier.compose_scoreboard(state)
    assert "⚠️ Live Roblox fetch failed" in text
    assert "snapshot-based sections still reflect the bot's own history" in text


def test_scoreboard_degrades_every_section():
    """A state with nothing in it renders explicit quiet lines everywhere."""
    state = {
        "name": "Unknown game", "playing": None, "visits": None,
        "favorites": None, "created_at": None, "state_stale": False,
        "growth": {}, "velocity": {}, "chart_positions": [],
        "chart_text": None, "ledger": {}, "verdict": None, "roots": None,
    }
    text = dossier.compose_scoreboard(state)
    assert "Dossier — Unknown game" in text
    assert "Not enough snapshot history" in text
    assert "Not on a Roblox chart" in text
    assert "Not alerted, watched, or radar-flagged" in text
    assert "Not analyzed yet" in text


# ---------------------------------------------------------------------------
# gather_ledger — the local-only ledger view
# ---------------------------------------------------------------------------

def test_gather_ledger_for_unknown_game_is_quiet(fresh_db):
    ledger = dossier.gather_ledger(db_path=None, game_id=424242)
    assert ledger["first_alerted"] is None
    assert ledger["watchers"] == 0
    assert ledger["ascent_last"] is None
    assert ledger["ascent_kind"] is None


def test_gather_ledger_counts_only_human_watchers(fresh_db):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name, source) "
        "VALUES (1, 50, 'W', 'user'), (0, 50, 'W', 'auto')"
    )
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (50, 'W')"
    )
    conn.commit()
    conn.close()

    ledger = dossier.gather_ledger(game_id=50)
    assert ledger["watchers"] == 1  # the auto row is NOT a human watcher
    assert ledger["first_alerted"] is not None


def test_gather_stored_verdict_roundtrip(fresh_db):
    assert dossier.gather_stored_verdict(99) is None

    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO ai_analysis_cache (game_id, verdict, confidence, "
        "strengths, risks, recommendation) "
        "VALUES (99, 'Solid pick', 80, 'fast growth', 'thin moderation', 'watch')"
    )
    conn.commit()
    conn.close()

    v = dossier.gather_stored_verdict(99)
    assert v["verdict"] == "Solid pick"
    assert v["confidence"] == 80


# ---------------------------------------------------------------------------
# build_dossier_view — the orchestrator (one Roblox call, fail-soft)
# ---------------------------------------------------------------------------

def test_view_survives_roblox_failure_with_local_data(fresh_db, monkeypatch):
    """Roblox down: still a full local report with a stale-state note."""
    import roblox_api

    # Seed the bot's own view of the game.
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (60, 'Ledger Name')"
    )
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (60, 100, 1000, datetime('now', '-2 hours'))"
    )
    conn.execute(
        "INSERT INTO game_history (game_id, players, visits, timestamp) "
        "VALUES (60, 140, 1100, datetime('now', '-30 minutes'))"
    )
    conn.commit()
    conn.close()

    async def failing_fetch(universe_id):
        raise RuntimeError("Roblox unreachable")

    monkeypatch.setattr(roblox_api, "get_game_info", failing_fetch)

    report = dossier.build_dossier_view(60)
    import asyncio
    text = asyncio.run(report)

    assert "Ledger Name" in text      # name resolved from the local ledger
    assert "Live Roblox fetch failed" in text  # stale-state note rendered
    assert "Window growth" in text    # local snapshot data still rendered
    assert "Scout read" in text       # narrative present


def test_view_uses_local_name_when_live_lacks_it(fresh_db, monkeypatch):
    import roblox_api

    async def minimal_fetch(universe_id):
        return {"playing": 70, "visits": 900, "favorites": 5,
                "creator": "", "created": "2025-01-01"}

    monkeypatch.setattr(roblox_api, "get_game_info", minimal_fetch)

    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (70, 'Local Title')"
    )
    conn.commit()
    conn.close()

    import asyncio
    text = asyncio.run(dossier.build_dossier_view(70))
    assert "Local Title" in text


# ---------------------------------------------------------------------------
# /dossier command surface
# ---------------------------------------------------------------------------

def test_dossier_command_is_registered():
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert 'name="dossier"' in source
    assert "build_dossier_view" in source  # real data path, not a stub
