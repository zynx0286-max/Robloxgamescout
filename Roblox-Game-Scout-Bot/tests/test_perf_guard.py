"""Performance guard: the bulk read path must stay meaningfully faster
than the per-game fallback path on the same synthetic store.

Self-relative (both paths timed back-to-back on the same machine, same
data), so the assertion is machine-independent — no absolute floor to
maintain, only the ratio. A ratio near 1.0 means a bulk layer has
regressed into per-game work; the scan pipeline's real stores run many
times faster than that.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # no-op guard

import database
import history
import velocity
import chart_rank

# Same store shape the bench script (and thus the committed baseline)
# uses: N_GAMES x SNAPSHOTS with the same charted fraction. Phase ratios
# are only comparable when the data shape is.
N_GAMES = 260
SNAPSHOTS = 6
CHARTED = 215

_BASELINE_PATH = Path(__file__).resolve().parent / "perf_baseline.json"
# Committed-ratio floor: one order of magnitude. A real regression (bulk
# layer collapsing into per-game work) shows up as ratios near 1x — far
# below a recorded ~30-190x. Timing runs best-of-3 per side, so one
# transient load spike (e.g. the rest of the suite running on the same
# machine) cannot fake a failure; anything that passes 3 quiet samples
# and still collapses is real.
REQUIRED_FRACTION_OF_BASELINE = 0.10


def _build() -> str:
    db_path = os.path.join(tempfile.mkdtemp(), "perf-guard.db")
    database.DATABASE = db_path
    # These modules snapshot the configured path at import, so redirect them
    # as well; otherwise the guard silently times an empty/default DB.
    history.DATABASE_PATH = db_path
    velocity.DATABASE_PATH = db_path
    conn = sqlite3.connect(db_path)
    conn.close()
    database.create_database()

    conn = sqlite3.connect(db_path)
    rows = []
    chart_rows = []
    for game_seq in range(1, N_GAMES + 1):
        for snap in range(1, SNAPSHOTS + 1):
            players = 100 + game_seq * snap
            rows.append((game_seq, players, players * 100))
    for game_seq in range(1, CHARTED + 1):
        for cycle in range(3):
            chart_rows.append((
                game_seq, f"sort_{cycle % 2}", f"Sort {cycle % 2}",
                (game_seq + cycle) % 50 + 1,
                f"2026-01-0{cycle + 1} 00:00:00.000000",
            ))
    conn.executemany(
        "INSERT INTO game_history (game_id, players, visits) VALUES (?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO chart_positions (game_id, sort_id, sort_label, position, cycle_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        chart_rows,
    )
    conn.commit()
    conn.close()
    return db_path


def _per_game_pass(ids, games):
    for gid in ids:
        history.growth_summary(gid)
    for gid in ids:
        velocity.get_historical_snapshots(gid)
    for g in games:
        chart_rank.attach_rank_signal(g)


def _bulk_pass(ids, games):
    history.growth_summaries_bulk(ids)
    velocity.compute_velocities_bulk(ids)
    chart_rank.attach_rank_signals_bulk(games)


def _time_best(fn, n=3):
    """Best wall-time over n samples — representative under load."""
    best = None
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


def _load_baseline():
    try:
        with open(_BASELINE_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload.get("phases"), payload.get("end_to_end")
    except (OSError, ValueError):
        return None, None


def test_bulk_reads_stay_faster_than_the_per_game_path():
    _build()
    ids = list(range(1, N_GAMES + 1))
    template = [
        {"id": gid, "chart_rank": 10, "charts_sort_id": "sort_0",
         "charts_sort": "Sort 0"}
        for gid in ids
    ]

    # Warm-up: class caches, SQLite page cache.
    _per_game_pass(ids, [dict(g) for g in template])
    _bulk_pass(ids, [dict(g) for g in template])

    # Best-of-3 per side so a transient load spike (the rest of the
    # suite, antivirus, CI neighbors) cannot fake a regression.
    per_game = _time_best(
        lambda: _per_game_pass(ids, [dict(g) for g in template])
    )
    bulk = _time_best(lambda: _bulk_pass(ids, [dict(g) for g in template]))

    assert bulk > 0
    # A regression toward per-game work collapses this ratio toward 1.0.
    # Historical throughput on the same phases: 241x on big stores, 59x
    # end-to-end incl. chart reads — 2x is a generous absolute floor.
    assert bulk * 2 < per_game, (
        f"bulk reads no longer beating per-game reads "
        f"(per_game={per_game:.3f}s bulk={bulk:.3f}s)"
    )

    # Phase-level ratios vs the COMMITTED baseline (machine-relative):
    # each phase must retain 10% of its recorded speedup — one order of
    # magnitude, catching any real bulk->per-game collapse while
    # tolerating hardware and load jitter on top of best-of-3 timing.
    baseline_phases, baseline_overall = _load_baseline()
    if baseline_overall:
        ratio = per_game / bulk
        assert ratio >= baseline_overall * REQUIRED_FRACTION_OF_BASELINE, (
            f"end-to-end bulk speedup collapsed vs the committed baseline "
            f"(now {ratio:.1f}x, baseline {baseline_overall:.1f}x)"
        )
    if baseline_phases:
        phase_ratios = _phase_ratios(ids, template)
        for phase, ratio_now in phase_ratios.items():
            recorded = baseline_phases.get(phase)
            if recorded:
                min_ratio = recorded * REQUIRED_FRACTION_OF_BASELINE
                assert ratio_now >= min_ratio, (
                    f"phase '{phase}' bulk speedup collapsed vs baseline "
                    f"(now {ratio_now:.1f}x, recorded {recorded:.1f}x)"
                )


def _phase_ratios(ids, template):
    """Per-phase old/bulk seconds, best-of-3 per side each."""
    def old_growth_fn():
        for gid in ids:
            history.growth_summary(gid)

    def bulk_growth_fn():
        history.growth_summaries_bulk(ids)

    def old_vel_fn():
        for gid in ids:
            velocity.get_historical_snapshots(gid)

    def bulk_vel_fn():
        velocity.compute_velocities_bulk(ids)

    def old_chart_fn():
        for g in template:
            chart_rank.attach_rank_signal(dict(g))

    def bulk_chart_fn():
        chart_rank.attach_rank_signals_bulk([dict(g) for g in template])

    return {
        "growth": _time_best(old_growth_fn) / _time_best(bulk_growth_fn),
        "velocity": _time_best(old_vel_fn) / _time_best(bulk_vel_fn),
        "chart_rank": _time_best(old_chart_fn) / _time_best(bulk_chart_fn),
    }
