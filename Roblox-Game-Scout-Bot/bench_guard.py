"""Bridge from the bot to scripts/bench_bulk_reads.py's pure helpers.

The bench file lives outside the import package; loading it by location
keeps one source of truth for the bench math (the module body defines
constants and functions only — `main()` is behind `if __name__ == "__main__"`
so exposing it here costs nothing). Everything is fail-soft: a missing
script, an unreadable baseline, or a format drift can never break the
/benchmark command — worst case is no drift lines attached.
"""

import importlib.util
import os

_BENCH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "scripts", "bench_bulk_reads.py"
)


def _load_bench_module():
    """A fresh import of scripts/bench_bulk_reads.py (None = unavailable)."""
    spec = importlib.util.spec_from_file_location("bench_bulk_reads", _BENCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bench_get_drift_lines(bench_stdout: str) -> list:
    """Drift lines for the /benchmark post ([] = clean or unavailable).

    The measured ratios carry BREADTH (which phases slid this run) and
    the recent perf history carries persistence, so every phase sliding
    together is one host-wide line (the machine, not one read path)
    while a single phase sliding alone routes to "fix the regression"
    when sustained or "rerun, suspect the host" when isolated — the
    post must never tell an operator to re-record a live regression
    into the baseline.
    """
    try:
        mod = _load_bench_module()
        ratios, _overall = mod.parse_bench_output(bench_stdout)
        baseline = mod.load_baseline()
        history = mod.load_history()
        return mod.drift_warnings(ratios, baseline, history=history)
    except Exception:
        return []


def bench_get_trend_lines(bench_stdout: str) -> list:
    """Repeat-drift lines ([] = no trends or unavailable).

    Same pure math as drift lines, but compared against the accumulated
    perf_history.jsonl and evaluated PER HOST: a host drifting in the
    scaled share (60%, fitted against AR(1) cluster noise) of its OWN
    recent runs earns a deliberate "this is repeat drift on that host"
    line — or ONE host-wide line when every phase drifted together, so
    the operator does not hunt for one read path on a slow machine.
    Never writes anything — the bench subprocess itself appends the
    history entry when it runs.
    """
    try:
        mod = _load_bench_module()
        ratios, _overall = mod.parse_bench_output(bench_stdout)
        baseline = mod.load_baseline()
        history = mod.load_history()
        return mod.drift_trend_lines(ratios, baseline, history)
    except Exception:
        return []


def bench_machine_lines():
    """The per-machine table for the /benchmark post ([] = one machine).

    /benchmark measures ONE host, so its drift and trend lines are about
    that host alone; with two machines in the log the other one's alert
    would never reach the channel. Same renderer /perfhistory uses
    (`host_status_text`), so the two surfaces cannot describe the same
    file differently. Fail-soft like the other bridges: [] on any error.
    """
    try:
        mod = _load_bench_module()
        return mod.host_status_text(mod.load_history(), mod.load_baseline())
    except Exception:
        return []


def bench_read_history_view(limit: int = 8):
    """Read-only accumulated-history text for /perfhistory.

    Never runs the bench and never writes a file — the whole point is
    "trend status without re-measuring". The text ends with the ROUTED
    ACTION for the latest logged run, rebuilt by the same two message
    functions /benchmark uses (fix the regressed read path / rerun,
    suspect host noise / rule out the host on a host-wide slide), so
    the read-only view gives the operator the same action without
    paying for a measurement. Returns the renderer's text, or None when
    the bridge fails (fail-soft, like the other bridges): the command
    posts an explicit "unavailable" message instead of inventing data.
    """
    try:
        mod = _load_bench_module()
        history = mod.load_history()
        baseline = mod.load_baseline()
        return mod.format_history(history, baseline, limit=limit)
    except Exception:
        return None
