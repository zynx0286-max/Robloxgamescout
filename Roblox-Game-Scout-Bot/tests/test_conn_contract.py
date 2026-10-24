"""Tests for the tranche contracts.

Layers:
* the sqlite_conn.py connection contract — every converted module's
  _connect() applies the same pragma set (hot paths and the later
  rare-path conversions alike), and tune() degrades quietly on a
  connection where pragmas fail; every module that touches the shared
  SQLite file now participates;
* the /benchmark drift check — parse the bench's own printed output,
  compare to the committed baseline, warn per phase below the floor.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite_conn
import database
import history
import velocity
import pulse
import scorecard
import ascent
import portfolio_db
import ai_scout_channel
import growth
import developer_intelligence
import gemini_analyzer
import ledger
import dossier


# ---------------------------------------------------------------------------
# Connection contract
# ---------------------------------------------------------------------------


def _pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_tune_applies_the_documented_pragmas(tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "x.db"))
    try:
        sqlite_conn.tune(conn)
        assert _pragma(conn, "busy_timeout") == 30000
        assert _pragma(conn, "synchronous") == 1  # NORMAL
        assert _pragma(conn, "temp_store") == 2   # MEMORY per SQLite's pragma table
    finally:
        conn.close()


def test_converted_modules_share_the_contract():
    """Every _connect() participates in one contract: fenced pragmas hold.

    Signatures differ by design (database/portfolio_db bind their own
    module-global path; the read helpers take the path), the pragmas
    must not."""
    zero_arg = {database, portfolio_db}
    path = os.path.join(tempfile.mkdtemp(), "contract.db")
    for mod in (
        database, history, velocity, pulse, scorecard, ascent, portfolio_db,
        ai_scout_channel, growth, developer_intelligence, gemini_analyzer,
        ledger, dossier,
    ):
        conn = mod._connect() if mod in zero_arg else mod._connect(path)
        try:
            assert _pragma(conn, "busy_timeout") == 30000, (
                f"{mod.__name__}._connect() is off-contract (busy_timeout)"
            )
            assert _pragma(conn, "synchronous") == 1, (
                f"{mod.__name__}._connect() is off-contract (synchronous)"
            )
        finally:
            conn.close()


def test_database_connect_still_carries_timeout_kwarg():
    """The bot's writer path keeps its own explicit busy-timeout arg too."""
    path = os.path.join(tempfile.mkdtemp(), "timeout.db")
    original = database.DATABASE
    database.DATABASE = path
    try:
        conn = database._connect()
        try:
            assert _pragma(conn, "busy_timeout") == 30000
        finally:
            conn.close()
    finally:
        database.DATABASE = original


# ---------------------------------------------------------------------------
# Drift check
# ---------------------------------------------------------------------------

_BENCH_STDOUT = (
    "database: /tmp/bench.db\n"
    "games: 260, snapshots/game: 6\n"
    "old (per-game conn+query) x2:   0.667s\n"
    "bulk (one query per metric) x2: 0.004s\n"
    "speedup[growth]: 180.0x\n"
    "speedup[velocity]: 120.0x\n"
    "speedup[chart_rank]: 140.0x\n"
    "speedup: 150.0x\n"
)

_BASELINE = {
    "phases": {"growth": 200.0, "velocity": 100.0, "chart_rank": 220.0},
    "end_to_end": 160.0,
}


def test_parse_bench_output_extracts_phases_and_overall():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bench_bulk_reads",
        str(Path(__file__).resolve().parents[1] / "scripts" / "bench_bulk_reads.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    phases, overall = mod.parse_bench_output(_BENCH_STDOUT)
    assert phases == {"growth": 180.0, "velocity": 120.0, "chart_rank": 140.0}
    assert overall == 150.0


def test_drift_flags_only_phases_below_floor():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bench_bulk_reads",
        str(Path(__file__).resolve().parents[1] / "scripts" / "bench_bulk_reads.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    warnings = mod.drift_warnings(
        {"growth": 180.0, "velocity": 120.0, "chart_rank": 140.0}, _BASELINE
    )
    # Floor = 25% of committed: growth 180 >= 50 ok, velocity 120 >= 25 ok,
    # chart_rank 140 >= 55 ok... all pass. Tighter: below-floor only.
    assert warnings == []

    warnings = mod.drift_warnings(
        {"growth": 40.0, "velocity": 120.0, "chart_rank": 140.0}, _BASELINE
    )
    # growth 40 < 200 * 0.25 = 50 -> drift; the others stay clean.
    assert len(warnings) == 1
    assert "'growth'" in warnings[0]
    # No history in this call: route to rerun, never to re-recording.
    assert "host noise first" in warnings[0]
    assert "--record" not in warnings[0]


def test_drift_silent_without_a_baseline():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bench_bulk_reads",
        str(Path(__file__).resolve().parents[1] / "scripts" / "bench_bulk_reads.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.load_baseline("/nonexistent/path.json") is None
    assert mod.drift_warnings({"growth": 1.0}, None) == []


def test_bench_guard_bridge_loads_the_real_script(tmp_path, monkeypatch):
    """the /benchmark bridge imports the bench module and returns lines."""
    import bench_guard

    # Real script + real committed baseline exist in this checkout —
    # exercise the bridge end to end. The sample output's all-above-floor
    # phases (committed: growth 183, velocity 166, chart 30) yield lines
    # only if genuinely drifted; chart_rank 140 >= 30*0.25 stays clean.
    lines = bench_guard.bench_get_drift_lines(_BENCH_STDOUT)
    assert isinstance(lines, list)
    # Whatever the committed history says, a flagged phase is routed to
    # fix-or-rerun; the benchmark post must never advise re-recording.
    assert all("--record" not in ln for ln in lines)
