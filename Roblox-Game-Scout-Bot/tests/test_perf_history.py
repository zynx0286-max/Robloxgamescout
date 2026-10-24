"""Tests for the perf history — pure functions, tmp files, no subprocess.

The perf history turns one-off drift alerts into a trend: every bench
run appends a dated JSONL entry, and `drift_trend_lines` flags a phase
only when it drifted repeatedly inside the recent window. Pinned here:
the entry shape, fail-soft loads, the cap/trim, and the exact window
semantics (one drift = noise, <window data points = silent, repeated
drift = line).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import bench_bulk_reads as bench


def _entry(phases, overall=150.0, ts="2026-10-18T00:00:00+00:00", recorded=False,
           host=None):
    return bench.history_entry(phases, overall, ts, recorded=recorded, host=host)


def test_history_entry_shape_rounds_ratios():
    entry = _entry({"growth": 123.456, "velocity": 2.0}, overall=99.987)
    assert entry["phases"] == {"growth": 123.46, "velocity": 2.0}
    assert entry["overall"] == 99.99
    assert entry["ts"] == "2026-10-18T00:00:00+00:00"
    assert entry["recorded"] is False


def test_append_and_load_round_trip(tmp_path):
    path = tmp_path / "perf_history.jsonl"
    e1 = _entry({"growth": 180.0}, ts="2026-10-18T01:00:00+00:00")
    e2 = _entry({"growth": 179.0}, ts="2026-10-18T02:00:00+00:00", recorded=True)

    assert bench.append_history(e1, path) is True
    assert bench.append_history(e2, path) is True

    rows = bench.load_history(path)
    assert [r["ts"] for r in rows] == [e1["ts"], e2["ts"]]
    assert rows[1]["recorded"] is True


def test_load_history_fail_soft(tmp_path):
    assert bench.load_history(tmp_path / "missing.jsonl") == []


def test_corrupt_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "perf_history.jsonl"
    good = _entry({"growth": 180.0}, ts="2026-10-18T01:00:00+00:00")
    bench.append_history(good, path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "half-written", "phases": {"g\n')

    rows = bench.load_history(path)
    assert len(rows) == 1
    assert rows[0]["ts"] == good["ts"]


def test_history_file_is_capped(tmp_path):
    """The file stays bounded: crossing HISTORY_KEEP trims to the newest
    TRIM_TO entries; appends after that re-grow until the next trim."""
    path = tmp_path / "perf_history.jsonl"
    for i in range(bench.HISTORY_KEEP + 20):
        bench.append_history(
            _entry({"growth": float(i)}, ts=f"2026-01-01T00:{i % 60:02d}:00+00:00"),
            path,
        )

    rows = bench.load_history(path)
    assert len(rows) <= bench.HISTORY_KEEP          # bounded, always
    assert len(rows) >= bench.HISTORY_TRIM_TO       # a trim HAS occurred
    # the trimmed file keeps the NEWEST entries
    assert rows[-1]["phases"]["growth"] == float(bench.HISTORY_KEEP + 19)


def baseline(phases):
    return {"phases": phases, "end_to_end": 160.0}


def test_trend_silent_without_baseline_or_history_or_ratios():
    hist = [_entry({"growth": 40.0})] * 5
    assert bench.drift_trend_lines({}, baseline({"growth": 200.0}), hist) == []
    assert bench.drift_trend_lines({"growth": 40.0}, None, hist) == []
    assert bench.drift_trend_lines({"growth": 40.0}, baseline({"growth": 200.0}), []) == []


def test_one_drift_is_noise_no_trend_line():
    hist = [
        _entry({"growth": 40.0}),                      # drifted
        _entry({"growth": 150.0}), _entry({"growth": 160.0}),
        _entry({"growth": 170.0}), _entry({"growth": 180.0}),
    ]
    assert bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist
    ) == []


def _ts(hour):
    return f"2026-10-18T{hour:02d}:00:00+00:00"


def test_repeated_drift_earns_a_trend_line():
    """Defaults: the slice is all one (unlabeled) host, n=10, threshold
    ceil(0.6 x 10) = 6 — 6 of its own runs below floor fires, and the
    line names what it is honest about: an unlabeled host."""
    ratios = [160.0, 170.0, 40.0, 30.0, 150.0, 35.0, 30.0, 160.0, 40.0, 30.0]
    hist = [_entry({"growth": g}, ts=_ts(10 + i)) for i, g in enumerate(ratios)]
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist
    )
    assert len(lines) == 1
    assert "growth" in lines[0]
    assert "6 of its last 10" in lines[0]
    assert "host unknown" in lines[0]


def test_two_run_transient_burst_stays_silent_at_defaults():
    """The whole point of scaling: two consecutive dips inside a full
    window (2 of 10 < ceil(0.6 x 10) = 6) are noise, not a trend."""
    ratios = [170.0] * 8 + [40.0, 35.0]
    hist = [_entry({"growth": g}, ts=_ts(10 + i)) for i, g in enumerate(ratios)]
    assert bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist
    ) == []


def test_default_threshold_scales_with_window():
    assert bench.default_trend_min_drifts(3) == 2    # ceil(1.8), floor 2
    assert bench.default_trend_min_drifts(5) == 3    # the old 5-window slice
    assert bench.default_trend_min_drifts(10) == 6   # the fitted default
    assert bench.default_trend_min_drifts(11) == 7   # ceil(6.6)
    # the scaling actually gates: a full 3-run slice fires at its own 2-of-3
    hist = [
        _entry({"growth": 170.0}, ts=_ts(10)),
        _entry({"growth": 40.0}, ts=_ts(11)),
        _entry({"growth": 30.0}, ts=_ts(12)),
    ]
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist, window=3
    )
    assert len(lines) == 1 and "2 of its last 3" in lines[0]


def test_thin_window_stays_silent():
    # only two data points for this phase: no trends from thin data
    hist = [_entry({"growth": 40.0}), _entry({"growth": 30.0})]
    assert bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist
    ) == []


def test_phases_missing_from_baseline_or_entries_are_skipped(tmp_path):
    hist = [_entry({"velocity": 40.0})] * 5          # no 'growth' recorded anywhere
    lines = bench.drift_trend_lines(
        {"growth": 40.0, "velocity": 40.0},
        baseline({"growth": 200.0}),                   # growth has a bar...
        hist,
    )
    # ...but 'velocity' has no committed bar and 'growth' no data points
    assert lines == []


def test_history_path_env_override(monkeypatch):
    """PERF_HISTORY_PATH moves the log — CI/test hygiene depends on it: the
    suite runs the bench for real and must not append to the committed file."""
    monkeypatch.delenv("PERF_HISTORY_PATH", raising=False)
    assert "perf_history.jsonl" in bench.effective_history_path()
    monkeypatch.setenv("PERF_HISTORY_PATH", str(Path("somewhere/else.jsonl")))
    assert bench.effective_history_path() == str(Path("somewhere/else.jsonl"))


def test_migrate_legacy_history_copies_once(tmp_path, monkeypatch):
    """The pre-data/ layout (tests/perf_history.jsonl) keeps its trend
    history: copied once into data/, never deleted, idempotent after."""
    legacy = tmp_path / "tests" / "perf_history.jsonl"
    legacy.parent.mkdir()
    legacy.write_text('{"ts":"2026-10-18T00:00:00+00:00"}\n', encoding="utf-8")
    target = tmp_path / "data" / "perf_history.jsonl"
    monkeypatch.setattr(bench, "_LEGACY_HISTORY_PATH", str(legacy))
    monkeypatch.setattr(bench, "PERF_HISTORY_PATH", str(target))
    monkeypatch.delenv("PERF_HISTORY_PATH", raising=False)

    assert bench.migrate_legacy_history() is True
    assert target.exists()                       # trend history preserved
    assert legacy.exists()                       # copied, never deleted
    assert bench.migrate_legacy_history() is False  # target now exists: idempotent


def test_migrate_never_fires_when_env_redirects(tmp_path, monkeypatch):
    """A custom PERF_HISTORY_PATH is operator-managed; migrating into it
    would surprise. Only the default location is migrated."""
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bench, "_LEGACY_HISTORY_PATH", str(legacy))
    monkeypatch.setattr(bench, "PERF_HISTORY_PATH", str(tmp_path / "data" / "h.jsonl"))
    monkeypatch.setenv("PERF_HISTORY_PATH", str(tmp_path / "custom.jsonl"))

    assert bench.migrate_legacy_history() is False
    assert not (tmp_path / "custom.jsonl").exists()


def test_format_history_empty_tells_the_user_to_start():
    assert "No bench runs recorded yet" in bench.format_history([], baseline({}))
    assert bench.format_history(None, None).startswith("No bench runs")


def test_format_history_renders_rows_lowest_run_marked():
    hist = [
        _entry({"growth": 100.0, "velocity": 50.0}, ts="2026-10-18T18:00:00+00:00"),
        _entry({"growth": 40.0, "velocity": 45.0}, ts="2026-10-18T19:00:00+00:00"),
    ]
    text = bench.format_history(hist, baseline({"growth": 200.0, "velocity": 100.0}))
    assert "runs: 2 shown of 2 logged" in text
    assert "latest: 2026-10-18T19:00:00+00:00" in text
    assert "committed baseline: growth 200.0x, velocity 100.0x" in text
    # growth 40 is below 200*0.25 → starred; velocity 45 is above 100*0.25 → not
    assert "40.0x⚠️" in text
    assert "45.0x⚠️" not in text and "45.0x" in text
    assert "100.0x" in text and "50.0x" in text
    # The view ends with the ROUTED action for that last run: growth is
    # below the floor and only once, so the operator is sent to rerun.
    assert "route for the latest logged run" in text
    assert "rerun, suspect host noise first" in text
    assert "--record" not in text


def test_format_history_limits_and_only_shows_recent(tmp_path):
    hist = [_entry({"growth": 100.0 + i}, ts=f"2026-10-1{d + 1}T00:00:00+00:00")
            for i, d in enumerate(range(6))]
    text = bench.format_history(hist, baseline({"growth": 200.0}), limit=3)
    assert "runs: 3 shown of 6 logged" in text
    assert "10-14 00:00" in text      # oldest shown entry
    assert "10-11 00:00" not in text  # dropped outside the 3-entry tail


def test_format_history_surfaces_repeat_drift_trend():
    ratios = [160.0, 170.0, 40.0, 30.0, 150.0, 35.0, 30.0, 160.0, 40.0, 30.0]
    deps = [_entry({"growth": g}, ts=_ts(10 + i)) for i, g in enumerate(ratios)]
    text = bench.format_history(deps, baseline({"growth": 200.0}))
    assert "route for the latest logged run" in text
    assert "fix the regressed read path" in text
    assert "growth" in text
    assert "6 of its last 10" in text
    assert "host unknown" in text  # unlabeled slices say exactly what they are
    assert "--record" not in text


def test_custom_window_and_min_drifts_count_correctly():
    hist = [
        _entry({"growth": 190.0}),
        _entry({"growth": 40.0}), _entry({"growth": 40.0}),
        _entry({"growth": 30.0}), _entry({"growth": 170.0}),
    ]
    # last 4 = [40, 40, 30, 170] → one unlabeled slice of 4, 3 drifts,
    # and the explicit min_drifts=3 threshold honored verbatim per host
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist,
        window=4, min_drifts=3,
    )
    assert len(lines) == 1
    assert "3 of its last 4" in lines[0]
    assert "host unknown" in lines[0]


# ---------------------------------------------------------------------------
# Host metadata: the point is telling "host switch" from "code regression"
# ---------------------------------------------------------------------------

_WIN = {"python": "3.14.0", "platform": "Windows-11-10.0.26100-SP0"}
_LIN = {"python": "3.12.3", "platform": "Linux-6.6.0-1012-aws"}


def test_history_entry_records_host_metadata():
    entry = _entry({"growth": 100.0}, host=_WIN)
    assert entry["host"] == _WIN


def test_history_entry_without_host_stays_legacy_compatible():
    entry = _entry({"growth": 100.0})
    assert "host" not in entry  # old files never gain a bogus key


def test_history_entry_host_is_truncated_and_never_empty(tmp_path):
    entry = _entry({"growth": 1.0}, host={"python": "x" * 99, "platform": ""})
    assert entry["host"]["python"] == "x" * 48
    assert "platform" not in entry["host"]
    path = tmp_path / "h.jsonl"
    assert bench.append_history(entry, path) is True
    rows = bench.load_history(path)
    assert rows[0]["host"]["python"] == entry["host"]["python"]


def test_host_window_is_per_machine():
    """Each host keeps its own last `window` runs: 12 runs on the other
    machine cannot evict this one's record, and the pooled view (the
    counterfactual) still returns the newest window overall."""
    hist = [_entry({"growth": 40.0}, host=_WIN) for _ in range(4)]
    hist += [_entry({"growth": 170.0}, host=_LIN) for _ in range(12)]
    own = bench.host_window(hist, (_WIN["platform"], _WIN["python"]))
    assert len(own) == 4                      # all of A's runs survive
    assert all(e["host"] == _WIN for e in own)
    lin = bench.host_window(hist, (_LIN["platform"], _LIN["python"]))
    assert len(lin) == bench.TREND_WINDOW
    pooled = bench.host_window(hist)
    assert len(pooled) == bench.TREND_WINDOW
    assert all(e["host"] == _LIN for e in pooled)   # 10 newest, all Linux


def test_trend_line_flags_a_mixed_host_window():
    """Per-host evaluation: evidence never pools across hosts. Each host
    speaks from its own slice (n=5 each, threshold ceil(0.6 x 5) = 3),
    but only the NEWEST run's host speaks a VERDICT — the last entry is
    Linux's, so Linux's line is the action and Windows' still-open alert
    is reported as context that cannot set one."""
    ratios = [40.0, 30.0, 160.0, 170.0, 40.0, 30.0, 160.0, 170.0, 40.0, 30.0]
    hist = [
        _entry({"growth": g}, ts=_ts(10 + i), host=_WIN if i % 2 == 0 else _LIN)
        for i, g in enumerate(ratios)
    ]
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist)
    verdicts = [l for l in lines if not l.startswith(bench.CONTEXT_MARK)]
    context = [l for l in lines if l.startswith(bench.CONTEXT_MARK)]
    assert len(verdicts) == 1 and len(context) == 1, lines
    assert "Linux" in verdicts[0] and "3 of its last 5" in verdicts[0]
    assert "Windows" in context[0] and "'growth' 3 of 5" in context[0]
    assert "Linux" in context[0] and "NOT this run" in context[0]
    assert bench.route_of(lines) == "fix"   # the other host never routes
    # the old mixed-window warning is gone: every line is host-scoped
    assert all("window mixes hosts" not in l for l in lines)


def test_unlabeled_run_cannot_speak_and_windows_line_fires():
    """One unlabeled entry cannot form a host slice (HOST_MIN_SLICE is
    3), so it contributes NO evidence — a host switch cannot fake a
    trend out of an unlabeled run — while the labeled Windows host,
    whose 9-entry slice holds six dips (= ceil(0.6 x 9) = 6), fires on
    its own measurements, without its neighbors' presence diluting it."""
    ratios = [40.0, 30.0, 40.0, 30.0, 40.0, 30.0, 160.0, 170.0, 150.0, 160.0]
    hist = [
        _entry({"growth": g}, ts=_ts(10 + i),
               host=None if i == 8 else _WIN)   # one unlabeled slice of n=1
        for i, g in enumerate(ratios)
    ]
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist)
    assert len(lines) == 1
    assert "6 of its last 9" in lines[0] and "Windows" in lines[0]


def test_trend_line_is_quiet_on_a_true_single_host_window():
    """All one host: its own 10-run slice holds the six dips, the line
    fires once and names the host — no mixing story, no dilution."""
    ratios = [160.0, 170.0, 40.0, 30.0, 150.0, 35.0, 30.0, 160.0, 40.0, 30.0]
    hist = [
        _entry({"growth": g}, ts=_ts(10 + i), host=_WIN)
        for i, g in enumerate(ratios)
    ]
    lines = bench.drift_trend_lines(
        {"growth": 150.0}, baseline({"growth": 200.0}), hist)
    assert len(lines) == 1
    assert "6 of its last 10" in lines[0] and "on Windows" in lines[0]


def test_format_history_legacy_reads_as_unknown_host():
    hist = [
        _entry({"growth": 100.0}, ts="2026-10-18T18:00:00+00:00", host=_WIN),
        _entry({"growth": 100.0}, ts="2026-10-18T19:00:00+00:00"),      # no host
    ]
    text = bench.format_history(hist, baseline({"growth": 200.0}))
    assert "Windows/py3.14.0" in text
    assert "?/py?" in text          # legacy entry reads as unknown host
    assert "hosts: Windows, unknown" in text  # sorted: uppercase first
    assert "compare within a host" in text


def test_format_history_single_host_has_no_caveat():
    hist = [
        _entry({"growth": 100.0}, ts="2026-10-18T18:00:00+00:00",
               host={"python": "3.14.0", "platform": "Windows-11-10.0.26100"}),
        _entry({"growth": 100.0}, ts="2026-10-18T19:00:00+00:00",
               host={"python": "3.13.0", "platform": "Windows-10-19045"}),
    ]
    text = bench.format_history(hist, baseline({"growth": 200.0}))
    assert "Windows/py3.14.0" in text and "Windows/py3.13.0" in text
    assert "hosts:" not in text   # one OS = never a machine switch


def test_format_history_calls_out_mixed_hosts():
    hist = [
        _entry({"growth": 100.0}, ts="2026-10-18T18:00:00+00:00", host=_WIN),
        _entry({"growth": 100.0}, ts="2026-10-18T19:00:00+00:00", host=_LIN),
    ]
    text = bench.format_history(hist, baseline({"growth": 200.0}))
    assert "hosts: Linux, Windows" in text
    assert "compare within a host" in text


def test_perfhistory_keeps_a_row_for_a_machine_that_stopped_running():
    """A host that has not run since the runs moved elsewhere has no \u26a0\ufe0f
    row and no line in the window, so without its own row its alert
    would read as resolved. Each row is that machine's own last verdict."""
    hist = [_entry({"growth": 190.0}, ts=_ts(i), host=_WIN) for i in range(6)]
    hist += [_entry({"growth": 30.0}, ts=_ts(10 + i), host=_WIN)
             for i in range(24)]
    hist += [_entry({"growth": 190.0}, ts=_ts(60 + i), host=_LIN)
             for i in range(12)]
    view = bench.format_history(hist, baseline({"growth": 200.0}))
    assert "per machine" in view
    lines = view.splitlines()
    win_row = next(ln for ln in lines if ln.startswith("Windows/py"))
    lin_row = next(ln for ln in lines if ln.startswith("Linux/py"))
    assert "fix the regressed read path (alert stuck)" in win_row
    assert "30" in win_row                      # its own run count
    assert "growth 10/10 low" in win_row        # ... and why, in its window
    assert "none" in lin_row
    assert "nothing below its floor" in lin_row
    assert lines.index(win_row) < lines.index(lin_row)   # quiet longest first
    # ... and the current run's verdict is still its own (B is clean)
    assert "route for the latest logged run" in view
    assert "no repeat drift" in view


def test_perfhistory_has_no_machine_table_for_one_host():
    hist = [_entry({"growth": 190.0}, ts=_ts(i), host=_WIN) for i in range(3)]
    view = bench.format_history(hist, baseline({"growth": 200.0}))
    assert "per machine" not in view


def test_reads_follow_the_history_redirect(tmp_path, monkeypatch):
    """The view and the trend check must read the file the bench WRITES:
    `PERF_HISTORY_PATH` used to redirect the writes only, so /perfhistory
    judged the committed file while the runs went elsewhere."""
    custom = tmp_path / "custom.jsonl"
    bench.append_history(_entry({"growth": 180.0}, ts=_ts(1)), custom)
    monkeypatch.setenv("PERF_HISTORY_PATH", str(custom))
    assert [r["ts"] for r in bench.load_history()] == [_ts(1)]
    assert bench.append_history(_entry({"growth": 181.0}, ts=_ts(2))) is True
    assert len(bench.load_history()) == 2
    assert str(custom) != bench.PERF_HISTORY_PATH


def test_host_status_rows_replay_each_machine_to_its_last_run():
    hist = [_entry({"growth": 190.0}, ts=_ts(1), host=_WIN),
            _entry({"growth": 30.0}, ts=_ts(2), host=_WIN),
            _entry({"growth": 30.0}, ts=_ts(3), host=_WIN),
            _entry({"growth": 30.0}, ts=_ts(4), host=_WIN),
            _entry({"growth": 190.0}, ts=_ts(5), host=_LIN)]
    rows = bench.host_status_rows(hist, baseline({"growth": 200.0}))
    assert [(r["key"][1], r["runs"]) for r in rows] == [("3.14.0", 4),
                                                         ("3.12.3", 1)]
    assert rows[0]["route"] == "fix"        # its own 3-of-4 lows
    assert rows[1]["route"] == "none"       # one healthy run, nothing open
    assert rows[0]["stuck"] is False        # saturation needs 11+
    assert rows[0]["evidence"] == "growth 3/4 low"
    assert rows[1]["evidence"] == "nothing below its floor"
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)


def test_host_row_evidence_keeps_breadth():
    """A host-wide slide must read as breadth, not as one phase: the row
    says every measured phase is low, with the worst count beside it."""
    hist = [_entry({"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
                   ts=_ts(i), host=_WIN) for i in range(10)]
    rows = bench.host_status_rows(
        hist, baseline({"growth": 200.0, "velocity": 180.0,
                        "chart_rank": 30.0}))
    assert rows[0]["route"] == "host"
    assert rows[0]["evidence"] == "all 3 phases low (worst chart_rank 10/10)"


def test_host_row_evidence_explains_an_aged_out_alert():
    """Route 'none' is not always a clean record: after a fix the window
    still holds lows, and the row says how many (that is why it is
    quiet, and how close it is to coming back)."""
    hist = [_entry({"growth": 22.0}, ts=_ts(i), host=_WIN) for i in range(10)]
    hist += [_entry({"growth": 196.0}, ts=_ts(20 + i), host=_WIN)
             for i in range(5)]
    rows = bench.host_status_rows(hist, baseline({"growth": 200.0}))
    assert rows[0]["route"] == "none"
    assert rows[0]["evidence"] == "growth 5/10 low"
