"""Routing contract for the drift warning and the repeat-drift line.

When a phase is flagged, the operator must be sent to the right action,
and WHICH phases slid decides it: a sustained slide in ONE phase with
the others at baseline routes to FIX (find the change in that read
path); EVERY measured phase sliding together routes to the HOST
(machine or shared path, host ruled out first) and never to one read
path; a single noisy dip routes to RERUN. NOTHING ever recommends
re-recording a live regression into the guarded baseline — that would
make CI forget the regression. The simulation in
scripts/trend_stress_test.py covers every noise mix x regression depth
x host-wide slide x concurrent-burst combination; these tests pin the
contract on the real message functions it calls.
"""

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import bench_bulk_reads as bench          # noqa: E402
from scripts import trend_stress_test as stress       # noqa: E402

PHASE = "growth"
PHASES = ("growth", "velocity", "chart_rank")
BASELINE = {"phases": {"growth": 200.0}, "end_to_end": 160.0}
BASELINE3 = {"phases": {"growth": 200.0, "velocity": 180.0,
                        "chart_rank": 30.0}, "end_to_end": 160.0}


def _hist(values):
    return [{"phases": {"growth": float(v)}} for v in values]


def _hist3(rows):
    """rows = [(growth, velocity, chart_rank), ...]"""
    return [{"phases": {"growth": float(g), "velocity": float(v),
                        "chart_rank": float(c)}} for g, v, c in rows]


def _healthy3(n, g=190.0, v=175.0, c=29.0):
    return _hist3([(g, v, c)] * n)


def test_sustained_slow_routes_to_fix_and_never_to_record():
    hist = _hist([40, 40, 30, 40, 40, 30, 170, 160, 150, 140])
    warns = bench.drift_warnings({"growth": 35.0}, BASELINE, history=hist)
    assert len(warns) == 1
    assert "REGRESSION" in warns[0]
    assert "fix" in warns[0]
    assert "--record" not in warns[0]


def test_isolated_dip_routes_to_rerun_not_fix():
    hist = _hist([170, 160, 150, 170, 160, 150, 170, 160, 150, 140])
    warns = bench.drift_warnings({"growth": 35.0}, BASELINE, history=hist)
    assert len(warns) == 1
    assert "host noise first" in warns[0]
    assert "REGRESSION" not in warns[0]
    assert "--record" not in warns[0]


def test_no_history_routes_to_rerun_and_never_to_record():
    warns = bench.drift_warnings({"growth": 35.0}, BASELINE)
    assert len(warns) == 1
    assert "host noise first" in warns[0]
    assert "--record" not in warns[0]


def test_trend_line_routes_to_fix_and_never_to_record():
    hist = _hist([40, 40, 30, 40, 40, 30, 170, 160, 150, 140])
    lines = bench.drift_trend_lines({"growth": 40.0}, BASELINE, hist)
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]
    assert "fix" in lines[0]
    assert "--record" not in lines[0]


def test_fix_route_requires_the_fitted_evidence_bar():
    """The fix route is not eager: two isolated lows inside a healthy
    window are still 'rerun', not a regression claim."""
    hist = _hist([170, 160, 150, 170, 40, 30, 160, 170, 150, 140])
    warns = bench.drift_warnings({"growth": 35.0}, BASELINE, history=hist)
    assert "REGRESSION" not in warns[0]


def _timeline(reg, noise, burst, seed, baseline, observed, floor, jitter=0.15,
              shared=None, phases=PHASES):
    rng = random.Random(f"route-test|{seed}|{reg}|{burst}|{shared}|{phases}")
    warm, tail = stress._routing_timeline(
        baseline, observed, floor, jitter, rng, reg, noise, burst, shared)
    history, routes, record_advice = [], [], 0
    for run in warm + tail:
        ratios = {p: round(run[p], 2) for p in phases}
        history.append({"phases": ratios, "host": stress.HOST})
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        record_advice += sum(1 for ln in lines if "--record" in ln)
        routes.append(stress._route_of(lines))
    return routes, record_advice


def test_real_regression_with_concurrent_burst_routes_to_fix_in_every_mix():
    """The request's matrix: truth = a real regression, with and without
    a concurrent host burst, under every noise mix. Every cell must end
    the timeline routed to FIX, and no message may recommend re-recording."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for label, level, rho in stress.ROUTING_NOISE:
        for reg in (0.20, 0.12, 0.06):
            for burst in (False, True):
                routes, record_advice = _timeline(
                    reg, (label, level, rho), burst, 0,
                    baseline, observed, floor)
                assert record_advice == 0, (label, reg, burst)
                assert "fix" in routes, (label, reg, burst, routes)


def test_noise_only_never_recommends_record_or_claims_regression_eagerly():
    """Negative control: with no regression underneath, a short host
    burst routes to RERUN, isolated iid dips never route to FIX, and no
    cell ever recommends re-recording. Heavy AR(1) clusters can satisfy
    the fitted evidence bar — indistinguishable from a regression — but
    even then the route is 'fix' (investigate), never 're-record'."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for label, level, rho in stress.ROUTING_NOISE:
        for burst in (False, True):
            routes, record_advice = _timeline(
                None, (label, level, rho), burst, 0,
                baseline, observed, floor)
            assert record_advice == 0, (label, burst)
            if label.startswith("iid") or label == "none":
                assert "fix" not in routes, (label, burst, routes)


def test_evidence_route_matches_the_fitted_trend_threshold():
    """The warning's 'sustained' bar is the trend check's own threshold,
    so the two messages cannot disagree about the same recent record."""
    for seen in range(1, bench.TREND_WINDOW + 1):
        for low in range(0, seen + 1):
            expected = (seen >= bench.HOST_MIN_SLICE
                        and low >= bench.default_trend_min_drifts(seen))
            assert bench.sustained_regression(low, seen) is expected


# ---------------------------------------------------------------------------
# Breadth: WHICH phases slid separates a host burst from a code regression.
# A host event lands on every phase at once; a code change lives in one
# phase's read path. The route must follow that evidence.
# ---------------------------------------------------------------------------


def test_phase_slide_counts_breadth_and_ignores_unbaselined_phases():
    ratios = {"growth": 10.0, "velocity": 175.0, "mystery": 0.1}
    slid, measured = bench.phase_slide(ratios, BASELINE3)
    assert slid == ["growth"]
    assert measured == 2  # 'mystery' has no committed bar to compare


def test_all_phases_sliding_together_routes_to_host_not_fix():
    """One run with every phase far under its baseline is a host event:
    ONE host-wide line that names all of them, not three phase
    regressions (which would send the operator hunting for three code
    changes on a machine that is simply slow)."""
    warnings = bench.drift_warnings(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, history=_healthy3(9))
    assert len(warnings) == 1
    assert "host-wide" in warnings[0]
    assert "REGRESSION" not in warnings[0]
    assert "--record" not in warnings[0]
    for phase in ("growth", "velocity", "chart_rank"):
        assert phase in warnings[0]


def test_sustained_shared_slide_routes_to_host_wide_not_one_path():
    """A persistent shared slide is still not one read path: the host
    (or a shared path every phase uses) is the suspect, and the host is
    ruled out first — while the re-record trap stays closed."""
    rows = [(190, 175, 29)] * 6 + [(30, 25, 4)] * 6
    warnings = bench.drift_warnings(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, history=_hist3(rows))
    assert len(warnings) == 1
    assert "host-wide" in warnings[0]
    assert "SHARED" in warnings[0]              # shared path, not N paths
    assert "Rule the host out first" in warnings[0]
    assert "REGRESSION" not in warnings[0]
    assert "--record" not in warnings[0]


def test_one_phase_sliding_alone_still_routes_to_fix():
    """The other phases at baseline is positive evidence FOR the phase:
    a narrow, sustained slide is a code regression and the message says
    why (it is not a host-wide slide)."""
    rows = [(190, 175, 29)] * 4 + [(30, 175, 29)] * 6
    warnings = bench.drift_warnings(
        {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0},
        BASELINE3, history=_hist3(rows))
    assert len(warnings) == 1
    assert "REGRESSION" in warnings[0]
    assert "only this phase slid this run" in warnings[0]
    assert "host-wide" not in warnings[0]
    assert "--record" not in warnings[0]


def test_one_phase_isolated_dip_reruns_and_does_not_blame_the_host():
    warnings = bench.drift_warnings(
        {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0},
        BASELINE3, history=_healthy3(9))
    assert len(warnings) == 1
    assert "host noise first" in warnings[0]
    assert "only this phase slid this run" in warnings[0]
    assert "REGRESSION" not in warnings[0]
    assert "--record" not in warnings[0]


def test_shared_slide_trend_line_is_one_host_wide_line():
    rows = [(190, 175, 29)] * 4 + [(30, 25, 4)] * 6
    hist = [dict(e, host=stress.HOST) for e in _hist3(rows)]
    lines = bench.drift_trend_lines(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, hist)
    assert len(lines) == 1
    assert "📈" in lines[0] and "drifted together" in lines[0]
    assert "host-wide" in lines[0]
    assert "REGRESSION" not in lines[0]
    assert "--record" not in lines[0]


def test_single_phase_trend_line_says_one_read_path_not_the_host():
    rows = [(190, 175, 29)] * 4 + [(30, 175, 29)] * 6
    hist = [dict(e, host=stress.HOST) for e in _hist3(rows)]
    lines = bench.drift_trend_lines(
        {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0},
        BASELINE3, hist)
    assert len(lines) == 1
    assert "REGRESSION" in lines[0]
    assert "one read path, not the host" in lines[0]
    assert "--record" not in lines[0]


def test_single_phase_runs_make_no_breadth_claim():
    """A run that measured one phase has no cross-phase evidence, so the
    message must not pretend the others stayed at baseline."""
    warnings = bench.drift_warnings({"growth": 30.0}, BASELINE,
                                    history=_healthy3(9, g=190.0))
    assert len(warnings) == 1
    assert "host noise first" in warnings[0]
    assert "only this phase" not in warnings[0]
    assert "host-wide" not in warnings[0]


def test_host_burst_on_every_phase_never_routes_to_fix():
    """Negative control with the burst ON THE WHOLE MACHINE: every phase
    slides for 3 onset runs, so no phase may be called a regression, and
    no message may recommend re-recording."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for label, level, rho in stress.ROUTING_NOISE:
        routes, record_advice = _timeline(
            None, (label, level, rho), True, 0, baseline, observed, floor)
        assert record_advice == 0, (label, routes)
        assert "fix" not in routes, (label, routes)


def test_sustained_host_wide_slide_never_routes_to_fix_or_record():
    """Truth = every phase slid together for the whole tail: the route
    must be host-wide in every noise mix, burst or not — never "fix one
    phase's read path" and never re-record."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for label, level, rho in stress.ROUTING_NOISE:
        for burst in (False, True):
            routes, record_advice = _timeline(
                None, (label, level, rho), burst, 0, baseline, observed,
                floor, shared=stress.ROUTING_SHARED)
            assert record_advice == 0, (label, burst)
            assert routes[-1] == "host", (label, burst, routes)
            assert "fix" not in routes, (label, burst, routes)


def test_single_phase_regression_survives_a_concurrent_host_burst():
    """The mixed case: a host burst (all phases) at onset on top of a
    real one-phase regression. The burst run may say host, but once it
    clears the narrow evidence must route to fix — and the final state
    of every timeline is fix."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for reg in (0.20, 0.12, 0.06):
        routes, record_advice = _timeline(
            reg, ("none", 0.0, None), True, 0, baseline, observed, floor)
        assert record_advice == 0
        assert routes[-1] == "fix", (reg, routes)
        assert routes[stress.ROUTING_WARMUP] in ("host", "fix"), reg


# ---------------------------------------------------------------------------
# A regression that DEEPENS over time: the route must escalate with the
# evidence — silence while above the floor, "rerun" while the dip is
# isolated, "fix" exactly when the fitted bar is met — and never re-record.
# ---------------------------------------------------------------------------


def _deepening(jit, seed, baseline, observed, floor):
    """(routes, bar_met_at_each_run, record_advice) for one ramp."""
    rng = random.Random(f"deepen-test|{jit}|{seed}")
    warm, tail = stress._deepening_timeline(baseline, observed, jit, rng)
    entries = [(None, run) for run in warm] + tail
    history, routes, met_at, record_advice = [], [], [], 0
    for _depth, run in entries:
        ratios = {p: round(v, 2) for p, v in run.items()}
        history.append({"phases": ratios, "host": stress.HOST})
        low, seen = bench.phase_evidence(PHASE, history, floor)  # absolute
        met = bench.sustained_regression(low, seen)
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        record_advice += sum(1 for ln in lines if "--record" in ln)
        routes.append(stress._route_of(lines))
        met_at.append(met)
    return routes, met_at, record_advice


def test_deepening_regression_escalates_exactly_on_the_fitted_bar():
    """No message before the floor, "rerun" while the dip is isolated,
    then "fix" on the FIRST run whose record satisfies the fitted bar —
    and at no jitter level does the route jump early, fall back to rerun
    once fixed, or recommend re-recording."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for jit in stress.DEEPENING_JITTERS:
        for seed in range(3):
            routes, met, record = _deepening(
                jit, seed, baseline, observed, floor)
            assert record == 0, (jit, seed)
            assert "fix" in routes, (jit, seed, routes)
            first_message = next(r for r in routes if r != "none")
            assert first_message == "rerun", (jit, seed, first_message)
            fix_idx = routes.index("fix")
            assert fix_idx == met.index(True), (jit, seed, fix_idx,
                                                met.index(True))
            assert "rerun" not in routes[fix_idx:], (jit, seed, routes)


def _recovery(jit, seed, baseline, observed, floor):
    """(alerting per run, index of the first recovered run, record
    advice, full history) for one fixed-regression recovery story."""
    rng = random.Random(f"recover-test|{jit}|{seed}")
    warm, tail, recovered = stress._recovery_timeline(
        baseline, observed, floor, jit, rng)
    history, alerting, record_advice = [], [], 0
    for run in warm + tail + recovered:
        ratios = {p: round(v, 2) for p, v in run.items()}
        history.append({"phases": ratios, "host": stress.HOST})
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        record_advice += sum(1 for ln in lines if "--record" in ln)
        alerting.append(bool(lines))
    return alerting, len(warm) + len(tail), record_advice, history


def test_alert_clears_five_healthy_runs_after_a_fixed_regression():
    """The 10-run window is the memory: after 24 regressed runs the
    alert is still up on the first healthy run, stops on the 5th (10
    window - 6 bar + 1), and never returns."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    alerting, onset, record, _history = _recovery(
        0.0, 0, baseline, observed, floor)
    assert alerting[onset - 1] is True          # regression was alerting
    after = alerting[onset:]
    assert after[0] is True                     # still up on the first
    cleared = after.index(False)
    assert cleared + 1 == 5                     # cleared on the 5th
    assert all(not a for a in after[cleared:])  # and stayed clear
    assert record == 0


def test_recovery_clears_and_perfhistory_returns_to_none():
    """At every jitter level and seed: the alert clears within the
    window, never comes back, /perfhistory routes back to "none", and
    nothing anywhere recommends re-recording."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for jit in stress.RECOVERY_JITTERS:
        for seed in range(3):
            alerting, onset, record, history = _recovery(
                jit, seed, baseline, observed, floor)
            after = alerting[onset:]
            assert record == 0, (jit, seed)
            assert after[0] is True, (jit, seed, after)
            cleared = after.index(False)
            assert cleared + 1 == 5, (jit, seed, cleared)
            assert all(not a for a in after[cleared:]), (jit, seed, after)
            view = bench.format_history(history, baseline)
            assert ("none — nothing below the floor and no repeat drift"
                    in view), (jit, seed)
            assert "--record" not in view


def _relapse(jit, seed, baseline, observed, floor):
    """(alerting, routes, history, stall start, relapse start, record
    advice) for one stall-then-relapse story."""
    rng = random.Random(f"relapse-test|{jit}|{seed}")
    warm, tail, stalled, relapsed = stress._relapse_timeline(
        baseline, observed, floor, jit, rng)
    history, alerting, routes, record = [], [], [], 0
    for run in warm + tail + stalled + relapsed:
        ratios = {p: round(v, 2) for p, v in run.items()}
        history.append({"phases": ratios, "host": stress.HOST})
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        record += sum(1 for ln in lines if "--record" in ln)
        alerting.append(bool(lines))
        routes.append(stress._route_of(lines))
    stall = len(warm) + len(tail)
    return alerting, routes, history, stall, stall + len(stalled), record


def test_partial_fix_ages_out_then_relapse_rearms_the_action():
    """A stall above the floor does not cancel the alert — the window
    ages it out on the 5th clear run, stays quiet, and a second
    regression re-arms the route through rerun to fix on the bar."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    alerting, routes, history, stall0, relapse0, record = _relapse(
        0.0, 0, baseline, observed, floor)
    stall = alerting[stall0:relapse0]
    assert stall[0] is True                    # memory keeps it up
    assert stall.index(False) + 1 == 5         # ages out, not cancelled
    assert all(not a for a in stall[5:])       # quiet for the rest
    rel = routes[relapse0:]
    assert rel[0] == "rerun"                   # second dip starts isolated
    assert rel.index("fix") + 1 == 6           # re-arms on the fitted bar
    assert rel[-1] == "fix"
    assert "fix the regressed read path" in bench.format_history(
        history, baseline)
    assert record == 0


def test_relapse_rearms_at_every_jitter_and_never_records():
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for jit in stress.RELAPSE_JITTERS:
        for seed in range(3):
            alerting, routes, history, stall0, relapse0, record = _relapse(
                jit, seed, baseline, observed, floor)
            stall = alerting[stall0:relapse0]
            rel = routes[relapse0:]
            assert record == 0, (jit, seed)
            assert stall[0] and stall.index(False) + 1 == 5, (jit, seed, stall)
            assert all(not a for a in stall[5:]), (jit, seed, stall)
            assert rel[0] == "rerun", (jit, seed, rel)
            assert rel.index("fix") + 1 == 6, (jit, seed, rel)
            assert rel[-1] == "fix", (jit, seed, rel)
            assert "fix the regressed read path" in bench.format_history(
                history, baseline), (jit, seed)


# ---------------------------------------------------------------------------
# A STUCK alert: once the window has been saturated for longer than its own
# length, repeating "find and fix" adds nothing — the same regression is
# still live and an applied fix is not holding.
# ---------------------------------------------------------------------------


def _stuck_run(jit, seed, depth, baseline, observed, floor):
    """One stuck-alert timeline: escalation onset, clearing and the
    evidence those were measured against."""
    rng = random.Random(f"stuck-test|{jit}|{seed}|{depth}")
    warm, regressed, recovered = stress._stuck_timeline(
        baseline, observed, jit, rng, depth)
    history, onset, clear_at, early = [], None, None, 0
    alerting_end, lows_at_end = False, None
    end_idx = len(warm) + len(regressed)
    for i, run in enumerate(warm + regressed + recovered):
        ratios = {p: round(v, 2) for p, v in run.items()}
        history.append({"phases": ratios, "host": stress.HOST})
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        if any("not holding" in ln for ln in lines) and onset is None:
            onset = i - len(warm) + 1
            if (bench.low_streak(PHASE, history, floor)
                    < bench.STUCK_MIN_STREAK):
                early += 1
        if i == end_idx - 1:
            alerting_end = bool(lines)
            window = [e for e in history[-bench.TREND_WINDOW:]
                      if (e.get("host") or {}).get("platform")
                      == stress.HOST["platform"]]
            lows_at_end = sum(1 for e in window
                              if e["phases"][PHASE] < floor)
        if i >= end_idx and clear_at is None and not lines:
            clear_at = i - end_idx + 1
    return {"onset": onset, "clear": clear_at, "early": early,
            "alerting_end": alerting_end, "lows": lows_at_end}


def test_stuck_alert_escalates_exactly_on_the_eleventh_low():
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    run = _stuck_run(0.0, 0, 0.12, baseline, observed, floor)
    assert run["onset"] == bench.STUCK_MIN_STREAK      # 11th consecutive low
    assert run["early"] == 0
    assert run["alerting_end"] is True
    assert run["clear"] == 5                          # 10 lows - 6 bar + 1


def test_stuck_alert_clears_in_the_recovery_window_at_every_jitter():
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    for jit in stress.STUCK_JITTERS:
        run = _stuck_run(jit, 0, 0.12, baseline, observed, floor)
        assert run["onset"] == bench.STUCK_MIN_STREAK, (jit, run)
        assert run["early"] == 0, (jit, run)
        assert run["clear"] == run["lows"] - 6 + 1, (jit, run)


def test_marginal_regression_never_escalates_early():
    """A regression near the floor flickers above it, which resets the
    streak: escalation may be delayed or never happen, but it must be
    earned (11+ consecutive lows) and the alert must still clear by the
    window arithmetic."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    escalated = 0
    for seed in range(10):
        run = _stuck_run(0.15, seed, 0.22, baseline, observed, floor)
        assert run["early"] == 0, (seed, run)
        if run["onset"] is not None:
            escalated += 1
            assert run["onset"] >= bench.STUCK_MIN_STREAK, (seed, run)
        if run["alerting_end"] and run["clear"] is not None:
            assert run["clear"] == max(1, run["lows"] - 6 + 1), (seed, run)
    assert escalated > 0   # the shape does reach the stuck alert sometimes


def test_saturated_window_escalates_to_a_stuck_alert():
    rows = [(190, 175, 29)] * 30 + [(30, 175, 29)] * 24
    hist = _hist3(rows)
    latest = {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0}
    warnings = bench.drift_warnings(latest, BASELINE3, history=hist)
    assert len(warnings) == 1
    assert "24 consecutive runs" in warnings[0]
    assert "not a new dip" in warnings[0]
    assert "not holding" in warnings[0]
    assert "REGRESSION" in warnings[0]        # route must stay fix
    assert "--record" not in warnings[0]

    lines = bench.drift_trend_lines(
        latest, BASELINE3, [dict(e, host=stress.HOST) for e in hist])
    assert len(lines) == 1
    assert "all 10 of its" in lines[0]
    assert "24 consecutive lows" in lines[0]
    assert "unresolved REGRESSION" in lines[0]
    assert "not repeat drift" in lines[0]
    assert "--record" not in lines[0]


def test_a_short_regression_repeats_the_fix_advice_not_the_stuck_alert():
    """Escalation is evidence-gated: six lows (bar just met) keep the
    original "find and fix" wording; only a saturated window escalates."""
    rows = [(190, 175, 29)] * 4 + [(30, 175, 29)] * 6
    warnings = bench.drift_warnings(
        {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0},
        BASELINE3, history=_hist3(rows))
    assert "REGRESSION" in warnings[0]
    assert "consecutive" not in warnings[0]
    assert "not holding" not in warnings[0]


def test_perfhistory_verdict_reports_the_stuck_alert():
    healthy = {"growth": 190, "velocity": 175, "chart_rank": 29}
    low = {"growth": 30, "velocity": 175, "chart_rank": 29}
    view = _view([healthy] * 30 + [low] * 15, BASELINE3)
    assert "fix the regressed read path — the alert is stuck" in view
    assert "15 consecutive runs" in view
    assert "not holding" in view
    assert "--record" not in view
    # and the non-stuck case still reads as the plain verdict
    view = _view([healthy] * 4 + [low] * 6, BASELINE3)
    assert "fix the regressed read path" in view
    assert "the alert is stuck" not in view
    assert "not holding" not in view


# ---------------------------------------------------------------------------
# The stuck alert across TWO hosts: the streak is counted in the regressed
# host's OWN runs, the line names that host, and it still ages out.
# ---------------------------------------------------------------------------

B_HOST = stress.HOST_B
B_PLATFORM = B_HOST["platform"]
A_PLATFORM = stress.HOST["platform"]
HEALTHY = {"growth": 190.0, "velocity": 175.0, "chart_rank": 29.0}
LOW = {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0}


def _two_host_entry(growth, host):
    return {"phases": {"growth": float(growth), "velocity": 175.0,
                        "chart_rank": 29.0}, "host": host}


def _interleaved(b_lows, b_healthy=0, pairs=6):
    """A/B alternating 1:1: B runs low `b_lows` times on its own runs,
    then healthy `b_healthy` times; A is healthy throughout."""
    hist = []
    for _ in range(pairs):
        hist += [_two_host_entry(190, stress.HOST),
                 _two_host_entry(190, B_HOST)]
    for _ in range(b_lows):
        hist += [_two_host_entry(190, stress.HOST),
                 _two_host_entry(30, B_HOST)]
    for _ in range(b_healthy):
        hist += [_two_host_entry(190, stress.HOST),
                 _two_host_entry(190, B_HOST)]
    return hist


def _two_host_lines(hist, latest):
    return (bench.drift_warnings(latest, BASELINE3, history=hist)
            + bench.drift_trend_lines(latest, BASELINE3, hist))


def test_stuck_alert_counts_the_regressed_hosts_own_runs():
    """A pooled count resets on the healthy host's every other run, so a
    machine low for 12 of its OWN runs must still escalate — and say
    which machine it is about."""
    lines = _two_host_lines(_interleaved(12), LOW)
    stuck = [ln for ln in lines if "not holding" in ln]
    assert len(stuck) == 2, lines         # the warning AND the trend line
    for ln in stuck:
        assert B_PLATFORM in ln           # names the regressed machine
        assert A_PLATFORM not in ln       # never the healthy one
        assert "12 consecutive" in ln
        assert "--record" not in ln


def test_stuck_alert_needs_eleven_of_the_hosts_own_lows():
    assert not any("not holding" in ln
                   for ln in _two_host_lines(_interleaved(10), LOW))
    assert any("not holding" in ln
               for ln in _two_host_lines(_interleaved(11), LOW))


def test_pooled_streak_could_not_escalate_an_interleaved_host():
    """The reason the count is per host: with host labels stripped the
    healthy host's entries break the streak every other run (1), so the
    same regression never escalates."""
    hist = _interleaved(12)
    for entry in hist:
        entry["host"] = None
    lines = _two_host_lines(hist, LOW)
    assert not any("not holding" in ln for ln in lines), lines
    assert any("rerun" in ln for ln in lines), lines   # still a dip to rerun


def test_every_warning_names_the_machine_it_measured():
    """Two hosts in the history make "this run" ambiguous: the plain
    REGRESSION and rerun lines must name the host too, not just the
    stuck alert, or the operator fixes the wrong box."""
    hist = [{"phases": {"growth": 190.0}, "host": stress.HOST}] * 4 \
        + [{"phases": {"growth": 30.0}, "host": stress.HOST}] * 6
    warns = bench.drift_warnings({"growth": 30.0}, BASELINE, history=hist)
    assert "REGRESSION" in warns[0]
    assert A_PLATFORM in warns[0]

    dip_hist = _interleaved(0) + [_two_host_entry(190, stress.HOST),
                                  _two_host_entry(30, B_HOST)]
    dipped = _two_host_lines(dip_hist, LOW)
    assert any("host noise first" in ln for ln in dipped), dipped
    for ln in dipped:
        assert B_PLATFORM in ln, ln
        assert A_PLATFORM not in ln, ln


def test_shared_slide_warning_names_the_host():
    rows = [(190, 175, 29)] * 9 + [(30, 25, 4)]
    hist = _hist3(rows)
    for entry in hist:
        entry["host"] = B_HOST
    warns = bench.drift_warnings(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, history=hist)
    assert len(warns) == 1
    assert "host-wide" in warns[0]
    assert B_PLATFORM in warns[0]
    assert A_PLATFORM not in warns[0]
    assert "--record" not in warns[0]


def _after_a_stuck_run(b_runs, b_growth):
    """A: 6 healthy then 24 low of its own (past saturation), then the
    runs move to B. B's runs sit on the tail of A's record inside the
    shared window, which is exactly what a host switch looks like to
    the detector."""
    hist = [_two_host_entry(190, stress.HOST) for _ in range(6)]
    hist += [_two_host_entry(30, stress.HOST) for _ in range(24)]
    hist += [_two_host_entry(b_growth, B_HOST) for _ in range(b_runs)]
    return hist


def test_a_clean_run_on_the_new_host_does_not_inherit_the_alert():
    """The verdict belongs to the machine that just measured: B's clean
    run routes 'none' while A's stuck alert is still open. The old code
    re-posted A's unresolved REGRESSION on that run, so the verdict for
    a healthy machine said "fix the regressed read path"."""
    lines = _two_host_lines(_after_a_stuck_run(1, 190), HEALTHY)
    verdicts = [ln for ln in lines if not ln.startswith(bench.CONTEXT_MARK)]
    assert verdicts == [], lines
    assert bench.route_of(lines) == "none"
    # ... and A's alert is still reported, as separate news for A.
    context = [ln for ln in lines if ln.startswith(bench.CONTEXT_MARK)]
    assert len(context) == 1, lines
    assert A_PLATFORM in context[0] and B_PLATFORM in context[0]
    assert "NOT this run's verdict" in context[0]
    # context can never set the action, whatever it reports
    assert bench.route_of(context) == "none"


def test_an_idle_host_comes_back_to_its_own_record():
    """The window is per machine: A's 24 low runs are still its record
    after 12 runs on B, so A's first run back is the saturated alert.
    Read pooled, the window held exactly ONE of A's runs, so the old
    rule could not say anything until A had run twice more."""
    hist = _after_a_stuck_run(12, 190)
    hist.append(_two_host_entry(30, stress.HOST))    # A returns, still low
    lines = _two_host_lines(hist, LOW)
    stuck = [ln for ln in lines if "not holding" in ln]
    assert stuck, lines
    assert all(A_PLATFORM in ln for ln in stuck), stuck
    assert not any(B_PLATFORM in ln for ln in lines), lines
    assert len([e for e in hist[-bench.TREND_WINDOW:]
                if e["host"] == stress.HOST]) == 1


def test_a_stuck_alert_is_earned_on_the_new_hosts_own_runs():
    """A's 24 lows must not hand B an escalation: B's first low run gets
    the ordinary dip advice, and the stuck wording waits until B has
    been low for 11 of its OWN runs."""
    first = _two_host_lines(_after_a_stuck_run(1, 30), LOW)
    assert not any("not holding" in ln for ln in first), first
    assert any(B_PLATFORM in ln for ln in first), first
    assert not any(A_PLATFORM in ln for ln in
                   [x for x in first if not x.startswith(bench.CONTEXT_MARK)])

    ten = _two_host_lines(_after_a_stuck_run(10, 30), LOW)
    assert not any("not holding" in ln for ln in ten), ten
    eleven = _two_host_lines(_after_a_stuck_run(11, 30), LOW)
    stuck = [ln for ln in eleven if "not holding" in ln]
    assert stuck and all(B_PLATFORM in ln for ln in stuck), eleven
    assert not any(A_PLATFORM in ln for ln in eleven), eleven


def _three(g, v, c, host):
    return {"phases": {"growth": float(g), "velocity": float(v),
                        "chart_rank": float(c)}, "host": host}


def test_narrow_persistence_is_per_host_too():
    """The per-run warning must agree with the per-host trend line: a
    host low on every one of its own runs is a REGRESSION, not "suspect
    host noise" — pooled, its 3 lows sit among 7 healthy runs from the
    other machine, which is below the 6-of-10 bar."""
    hist = [_three(190, 175, 29, stress.HOST) for _ in range(4)]
    for _ in range(2):
        hist += [_three(30, 25, 4, B_HOST),
                 _three(190, 175, 29, stress.HOST)]
    hist.append(_three(30, 25, 4, B_HOST))       # this run, on B
    warns = bench.drift_warnings(LOW, BASELINE3, history=hist)
    assert len(warns) == 1, warns
    assert "REGRESSION" in warns[0]
    assert "host noise first" not in warns[0]
    assert "low in 3 of that host's last 3" in warns[0]
    assert B_PLATFORM in warns[0] and A_PLATFORM not in warns[0]
    assert "--record" not in warns[0]


def test_shared_slide_persistence_counts_the_hosts_own_runs():
    """A host sliding on every one of its own runs is SUSTAINED even
    when a healthy second host fills half the window — pooled that is
    5 of 10, below the 6 bar, so the old rule called it transient."""
    hist = []
    for _ in range(6):
        hist += [_three(190, 175, 29, stress.HOST),
                 _three(190, 175, 29, B_HOST)]
    for _ in range(12):
        hist += [_three(190, 175, 29, stress.HOST),
                 _three(30, 25, 4, B_HOST)]
    warns = bench.drift_warnings(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, history=hist)
    assert len(warns) == 1, warns
    assert "this MACHINE" in warns[0]          # sustained, not "rerun"
    # its own last 10 runs, not the 5 it used to get from a shared window
    assert "low in 10 of that host's last 10" in warns[0]
    assert B_PLATFORM in warns[0] and A_PLATFORM not in warns[0]
    assert "--record" not in warns[0]


def test_shared_slide_on_a_brand_new_host_stays_transient():
    """Two runs cannot claim persistence, and the message says how thin
    the evidence is instead of asserting a machine-wide slide."""
    hist = [_three(190, 175, 29, stress.HOST) for _ in range(10)]
    hist += [_three(30, 25, 4, B_HOST)] * 2
    warns = bench.drift_warnings(
        {"growth": 30.0, "velocity": 25.0, "chart_rank": 4.0},
        BASELINE3, history=hist)
    assert len(warns) == 1
    assert "low in 2 of that host's last 2" in warns[0]
    assert "Rerun /benchmark when the box is quiet" in warns[0]
    assert "this MACHINE" not in warns[0]
    assert "--record" not in warns[0]


def test_perfhistory_verdict_calls_the_interleaved_host_stuck():
    """The read-only view summarises the SAME messages it prints: a
    pooled count would route "fix" here while omitting that the alert
    is stuck — and the stuck line below it would contradict it."""
    pairs = _interleaved(12)
    hist = [{"ts": f"2026-10-22T00:{i // 60:02d}:{i % 60:02d}+00:00",
             "phases": e["phases"], "host": e["host"]}
            for i, e in enumerate(pairs)]
    view = bench.format_history(hist, BASELINE3)
    assert "the alert is stuck" in view
    assert "12 consecutive runs" in view
    assert B_PLATFORM in view       # both the verdict's messages name it
    assert A_PLATFORM not in view
    assert "--record" not in view


def test_stuck_alert_clears_after_the_regressed_hosts_healthy_runs():
    """B's own 10-run window at the 6-of-10 bar: 12 lows then healthy
    runs keep it alerting until 5 lows of 10 fall below the bar, the
    same arithmetic a single-host history gets."""
    for healthy in (0, 1, 2, 3, 4):
        lines = _two_host_lines(_interleaved(12, healthy),
                                HEALTHY if healthy else LOW)
        assert lines, healthy
    assert _two_host_lines(_interleaved(12, 5), HEALTHY) == []


def test_route_classifier_is_one_contract_for_every_surface():
    assert bench.route_of(["… treat it as a REGRESSION: fix …"]) == "fix"
    assert bench.route_of(["… host noise first: rerun …"]) == "rerun"
    assert bench.route_of(["… a shared slide is host-wide …"]) == "host"
    assert bench.route_of([]) == "none"


def _view(rows, baseline):
    """format_history over per-phase rows (oldest first, one host)."""
    hist = [{"ts": f"2026-10-{i + 1:02d}T00:00:00+00:00",
             "phases": {p: float(v) for p, v in row.items()},
             "host": stress.HOST} for i, row in enumerate(rows)]
    return bench.format_history(hist, baseline)


def test_perfhistory_view_routes_a_narrow_sustained_slide_to_fix():
    """The read-only view must post the SAME action /benchmark would: a
    narrow, sustained slide is a regression to fix, and the exact
    messages are present (not a paraphrase)."""
    rows = [{"growth": 190, "velocity": 175, "chart_rank": 29}] * 4 \
        + [{"growth": 30, "velocity": 175, "chart_rank": 29}] * 6
    latest = {"growth": 30.0, "velocity": 175.0, "chart_rank": 29.0}
    hist = [{"ts": f"2026-10-{i + 1:02d}T00:00:00+00:00",
             "phases": {p: float(v) for p, v in row.items()},
             "host": stress.HOST} for i, row in enumerate(rows)]
    expected = bench.drift_warnings(latest, BASELINE3, history=hist)
    expected += bench.drift_trend_lines(latest, BASELINE3, hist)
    assert bench.route_of(expected) == "fix"

    view = bench.format_history(hist, BASELINE3)
    assert "route for the latest logged run" in view
    assert "fix the regressed read path" in view
    for ln in expected:
        assert ln in view, ln          # identical messages, not a summary
    assert "--record" not in view


def test_perfhistory_view_routes_an_isolated_dip_to_rerun():
    rows = [{"growth": 190, "velocity": 175, "chart_rank": 29}] * 9 \
        + [{"growth": 30, "velocity": 175, "chart_rank": 29}]
    view = _view(rows, BASELINE3)
    assert "rerun, suspect host noise first" in view
    assert "fix the regressed read path" not in view
    assert "REGRESSION" not in view
    assert "--record" not in view


def test_perfhistory_view_routes_a_shared_slide_to_the_host():
    rows = [{"growth": 190, "velocity": 175, "chart_rank": 29}] * 9 \
        + [{"growth": 30, "velocity": 25, "chart_rank": 4}]
    view = _view(rows, BASELINE3)
    assert "rule the host out first (host-wide slide)" in view
    assert "host-wide" in view
    assert "REGRESSION" not in view
    assert "--record" not in view


def test_perfhistory_view_says_none_when_there_is_nothing_to_do():
    rows = [{"growth": 190, "velocity": 175, "chart_rank": 29}] * 3
    view = _view(rows, BASELINE3)
    assert "none — nothing below the floor and no repeat drift" in view
    assert "REGRESSION" not in view and "rerun" not in view


def test_perfhistory_explains_stale_warning_marks():
    """A clean verdict over a table with ⚠️ marks must not look like a
    contradiction: say the marks are history."""
    healthy = {"growth": 190, "velocity": 175, "chart_rank": 29}
    low = {"growth": 30, "velocity": 175, "chart_rank": 29}
    view = _view([low, healthy, healthy], BASELINE3)
    assert "none — nothing below the floor and no repeat drift" in view
    assert "note: 1 earlier run in this view was flagged" in view
    view = _view([low, low, healthy, healthy], BASELINE3)
    assert "note: 2 earlier runs in this view were flagged" in view


def test_perfhistory_counts_down_the_recovery_after_a_fix():
    """While the latest run is healthy but the window still holds lows,
    the view says the alert is ageing out and how many healthy runs are
    left — matching the measured 5-run recovery budget."""
    healthy = {"growth": 190, "velocity": 175, "chart_rank": 29}
    low = {"growth": 30, "velocity": 175, "chart_rank": 29}
    view = _view([low] * 10 + [healthy], BASELINE3)
    assert "fix the regressed read path" in view
    assert "recovery: still alerting from the window memory" in view
    assert "4 more healthy runs clear it." in view     # 9 lows - 6 bar + 1

    view = _view([low] * 10 + [healthy] * 4, BASELINE3)
    assert "1 more healthy run clears it." in view

    view = _view([low] * 10 + [healthy] * 5, BASELINE3)
    assert "none — nothing below the floor and no repeat drift" in view
    assert "recovery:" not in view
    assert "earlier run" in view and "flagged (⚠️)" in view


def test_perfhistory_view_without_a_baseline_routes_nothing():
    rows = [{"growth": 190}] * 2
    view = _view(rows, None)
    assert "no committed baseline to compare against" in view


def test_deepening_without_noise_pins_rerun_then_fix_five_runs_later():
    """Deterministic budget: the first below-floor run is an isolated
    dip (rerun); the bar is 6 lows of the last 10, so the fix lands five
    lows later — the documented escalation, measured on the real
    messages rather than assumed."""
    baseline, observed = stress.load_corpus()
    floor = baseline["phases"][PHASE] * stress.FLOOR
    routes, met, record = _deepening(0.0, 0, baseline, observed, floor)
    assert record == 0
    rerun_idx = routes.index("rerun")
    fix_idx = routes.index("fix")
    assert fix_idx - rerun_idx == 5
    assert met[fix_idx] and not any(met[:fix_idx])
    assert all(r == "none" for r in routes[:rerun_idx])
