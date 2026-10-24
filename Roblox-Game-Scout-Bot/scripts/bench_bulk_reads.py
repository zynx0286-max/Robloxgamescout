"""Measure the bulk-read win in history/velocity on a populated store.

Run from the repo root:
    .venv/Scripts/python.exe Roblox-Game-Scout-Bot/scripts/bench_bulk_reads.py

Builds synthetic game_history data (N games x K snapshots), then times the
old read pattern (per-game connection + query) against the new bulk query
per phase and end-to-end. Writes nothing to production data — a throwaway
temp database is used.

`--record` additionally commits the per-phase speedups into
`tests/perf_baseline.json` (the commit-time machine's numbers). The
perf guard then compares future runs against that committed baseline,
so a bulk layer that quietly regresses toward per-game work fails in
code review instead of months later on a live host.

EVERY run - recorded or not - also appends one dated entry to
`data/perf_history.jsonl` (`append_history`). That log is what turns
single drift alerts into a trend:`drift_trend_lines` looks at each
host's own last TREND_WINDOW entries and flags a
phase that drifted in TREND_MIN_DRIFTS_FRACTION of them (ceil of the
share x window, at least 2 — 60% as fitted against the AR(1) cluster
noise model), so transient stretches on a busy host can't fake a
trend, while a genuinely degrading phase still fires within about six
runs. The trend check evaluates PER HOST PLATFORM: each line counts
and names one host's own runs, so a host switch can neither fake
evidence pooled across hosts nor mask one host's regression behind
another host's healthy entries. A line is a claim about the run that
carries it: only the newest run's host speaks a verdict, while another
machine's still-open alert is reported as a `CONTEXT_MARK` note that
`route_of` never turns into an action.
"""

import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

N_GAMES = 260          # a heavy scan cycle's collection volume
SNAPSHOTS_PER_GAME = 6  # one cycle-day at a 30-min interval
BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "tests", "perf_baseline.json"
)
PERF_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "data", "perf_history.jsonl"
)

# Trend detection gives each host its OWN last WINDOW runs (unlabeled
# runs group as "unknown") and a host earns a trend line when
# TREND_MIN_DRIFTS_FRACTION of that slice sits below the committed
# floor — threshold = ceil(share x slice), floor 2, from
# >= HOST_MIN_SLICE of the host's own runs. Per-host scoping is the
# fix, scaling the burst-robustness lever: a mixed history can neither
# fake (no pooling across hosts) nor mask (one host's dips fire from
# its slice alone) a regression, and the WINDOW is per machine too, so
# a host is never left with a fraction of a window because another
# host has been running (its own 10 runs are its record, whether it
# ran them a minute or a week ago).
#
# The 60% share is FIT, not guessed: scripts/trend_stress_test.py
# --refit grid-searches (window x share) against an AR(1) CLUSTER
# noise model (autocorrelated slow stretches — the dependence a real
# host has; the retired burst+idle model hand-shaped it) and picks by
# a pre-declared objective: minimise the worst ordinary-noise band
# (marginal dip rates <=10%, rho 0.5/0.8) subject to a 6-run detection
# budget. At window 10 that fit lands on 60% (6 of 10): worst-band
# false fires fall from 70% (the old 40% policy) to 45%, and every
# candidate that does better needs a longer wait than 6 regressed
# runs. Cost, measured and deliberate: a sustained regression fires on
# the 6th bad run, not the 4th. Window 10 alone still holds for the
# thinner per-host slice: a 5-run slice needs 3 dips, 3 needs 2.
TREND_WINDOW = 10
TREND_MIN_DRIFTS_FRACTION = 0.6
HOST_MIN_SLICE = 3   # a host speaks from >=3 of its own runs, never fewer
# A phase low in every run of the window AND at least one older run has
# been at the detector's ceiling for a full window: repeating "find and
# fix" adds no information, so the message ESCALATES to a stuck alert
# (an applied fix is not holding). One past the window is the smallest
# streak that proves the evidence is saturated rather than just full.
# The streak is counted in the REGRESSED HOST's own runs, never pooled:
# with two hosts interleaved, a healthy second host's entries reset a
# pooled count every time it logs, so the same machine's unresolved
# regression could sit there forever without ever escalating.
STUCK_MIN_STREAK = TREND_WINDOW + 1
# A line that reports ANOTHER machine's still-open alert is context,
# not this run's verdict: it carries this mark and `route_of` ignores
# it. Without the split, a healthy run on host B re-posts host A's
# unresolved regression as though B had measured it, and the verdict
# for a clean run says "fix the regressed read path".
CONTEXT_MARK = "ℹ️"
# The history file is rewritten (not append-only) once it exceeds KEEP:
# 200 runs is a lot of drift evidence, and rewriting keeps the file in
# reviewable shape instead of growing forever on a long-lived host.
HISTORY_KEEP = 200
HISTORY_TRIM_TO = 100


def build_store() -> str:
    import database
    import history
    import sqlite3
    import velocity

    db_path = os.path.join(tempfile.mkdtemp(), "bench.db")
    database.DATABASE = db_path
    # history/velocity snapshot the configured path at import; point them at
    # the throwaway store too, or the bench would time (and blame) whichever
    # database the environment happens to name.
    history.DATABASE_PATH = db_path
    velocity.DATABASE_PATH = db_path
    conn = sqlite3.connect(db_path)
    conn.close()
    database.create_database()

    conn = sqlite3.connect(db_path)
    rows = []
    chart_rows = []
    for game_seq in range(1, N_GAMES + 1):
        for snap in range(1, SNAPSHOTS_PER_GAME + 1):
            players = 100 + game_seq * snap
            rows.append((game_seq, players, players * 100))
    # Charted-subset seeding: the ARCHITECTURE.md cycle estimate (~124
    # charted of ~150 collected) scaled to the bench's 260 games.
    charted_games = max(1, int(N_GAMES * 124 / 150))
    for game_seq in range(1, charted_games + 1):
        for cycle in range(3):  # a couple of prior cycles for delta math
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


def bench_old_path(ids, game_dicts):
    """One connection + query per game — the pre-change read pattern."""
    from history import growth_summary
    from velocity import get_historical_snapshots
    import chart_rank
    t0 = time.perf_counter()
    for gid in ids:
        growth_summary(gid)
    for gid in ids:
        get_historical_snapshots(gid)
    for game in game_dicts:
        chart_rank.attach_rank_signal(game)
    return time.perf_counter() - t0


def bench_bulk_path(ids, game_dicts):
    from history import growth_summaries_bulk
    from velocity import compute_velocities_bulk
    import chart_rank
    t0 = time.perf_counter()
    growth_summaries_bulk(ids)
    compute_velocities_bulk(ids)
    chart_rank.attach_rank_signals_bulk(game_dicts)
    return time.perf_counter() - t0


def _timed(fn, args, repeats=3):
    best = None
    for _ in range(repeats):
        dt = fn(*args)
        best = min(dt, best if best is not None else dt)
    return best


def bench_per_phase(ids, game_dicts):
    """Per-phase speedups as {phase: old_seconds / bulk_seconds}.

    Old and bulk are timed back-to-back on the same store per phase,
    so the ratio is machine-independent (same self-relative logic the
    perf guard uses).
    """
    import chart_rank
    from history import growth_summary, growth_summaries_bulk
    from velocity import compute_velocities_bulk, get_historical_snapshots

    phases = {}

    t0 = time.perf_counter()
    for gid in ids:
        growth_summary(gid)
    old_growth = time.perf_counter() - t0
    t0 = time.perf_counter()
    growth_summaries_bulk(ids)
    bulk_growth = time.perf_counter() - t0
    phases["growth"] = old_growth / bulk_growth

    t0 = time.perf_counter()
    for gid in ids:
        get_historical_snapshots(gid)
    old_vel = time.perf_counter() - t0
    t0 = time.perf_counter()
    compute_velocities_bulk(ids)
    bulk_vel = time.perf_counter() - t0
    phases["velocity"] = old_vel / bulk_vel

    t0 = time.perf_counter()
    for game in game_dicts:
        chart_rank.attach_rank_signal(game)
    old_chart = time.perf_counter() - t0
    t0 = time.perf_counter()
    chart_rank.attach_rank_signals_bulk(game_dicts)
    bulk_chart = time.perf_counter() - t0
    phases["chart_rank"] = old_chart / bulk_chart

    return phases


def parse_bench_output(text):
    """Extract the printed speedups from a bench stdout.

    The /benchmark command posts this output raw; the drift check needs
    the same numbers the human eyes see. Returns ({phase: ratio},
    overall_ratio)."""
    import re

    phases = {}
    overall = None
    for line in text.splitlines():
        m = re.search(r"speedup\[(\w+)\]:\s*([0-9.]+)x", line)
        if m:
            phases[m.group(1)] = float(m.group(2))
            continue
        m = re.search(r"speedup:\s*([0-9.]+)x", line)
        if m and overall is None:
            overall = float(m.group(1))
    return phases, overall


def load_baseline(path=None):
    """The committed baseline, or None when it is not shipped."""
    try:
        with open(path or BASELINE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def host_window(history, host_key=None, window=TREND_WINDOW):
    """The newest `window` entries that belong to `host_key` (newest first).

    The window is per MACHINE: each host keeps its own last `window`
    runs, so another host's runs cannot push this one's evidence out of
    it. A host that has been idle for longer than the window still owns
    its record, and comes back to a full window of its own runs instead
    of an empty slice (the shared-window version had to refill it).

    `host_key=None` returns the newest `window` entries overall, the
    pooled view the counterfactual columns measure (every host counted
    together, which is exactly what per-host scoping exists to avoid).
    """
    out = []
    for entry in reversed(history or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("phases"),
                                                        dict):
            continue
        if host_key is not None and host_key_of(entry) != host_key:
            continue
        out.append(entry)
        if len(out) >= window:
            break
    return out


def phase_evidence(phase, history, threshold, window=TREND_WINDOW,
                   host_key=None):
    """(low, seen) for `phase` across the recent runs that recorded it.

    `threshold` is the absolute drift floor (committed x floor). Used to
    tell a SUSTAINED slow phase from a single noisy dip — the difference
    between "fix the code" and "rerun, suspect the host".

    With a `host_key`, the tail is that host's OWN last `window` runs
    (`host_window`) — the same scoping `low_streak` and
    `drift_trend_lines` use, so persistence is a claim about ONE
    machine's record, read at the same fitted bar a single machine
    gets.
    """
    tail = []
    for entry in host_window(history, host_key, window):
        if isinstance(entry["phases"].get(phase), (int, float)):
            tail.append(entry)
    return (sum(1 for e in tail if e["phases"][phase] < threshold),
            len(tail))


def sustained_regression(low, seen):
    """True when the recent record is slow at the SAME bar the trend
    check uses: at least HOST_MIN_SLICE observations, and low dips >=
    `default_trend_min_drifts` of them (the fitted scaled threshold).

    Sharing the rule keeps the per-run warning and the 📈 trend line on
    the same bar (the trend additionally scopes evidence per host, so
    after a host switch the two can legitimately differ): a 1-4 run
    host burst never reaches it; a real regression reaches it on the
    same run the trend fires.
    This is PERSISTENCE only — a long NARROW slide is a regression,
    while the same persistence with every phase sliding together is
    host-wide and is routed by breadth (`drift_warnings`,
    `_shared_slide_warning`).
    """
    return seen >= HOST_MIN_SLICE and low >= default_trend_min_drifts(seen)


def low_streak(phase, history, threshold, host_key=None):
    """Consecutive runs at the END of `history` below `threshold`.

    With a `host_key`, only that host's runs count (other hosts' entries
    are skipped, not treated as highs), matching the per-host scoping of
    `drift_trend_lines`. Returns 0 when the newest entry is not low.
    """
    streak = 0
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            break
        if host_key is not None:
            h = entry.get("host") or {}
            if ((h.get("platform") or None),
                    (h.get("python") or None)) != host_key:
                continue
        v = (entry.get("phases") or {}).get(phase)
        if not isinstance(v, (int, float)) or v >= threshold:
            break
        streak += 1
    return streak


def host_key_of(entry):
    """(platform, python) of one history entry (None members = unlabeled).

    One place builds the key, so every consumer — the per-host window,
    the trend slices, the stuck streak, /perfhistory's rows — groups the
    runs identically. An entry with no host metadata is the `(None,
    None)` group ("unknown"), never silently folded into a real host.
    """
    host = (entry.get("host") or {}) if isinstance(entry, dict) else {}
    return ((host.get("platform") or None), (host.get("python") or None))


def latest_host_key(history):
    """(platform, python) of the NEWEST run in `history`, or None.

    The stuck escalation is about the machine that just ran, so it uses
    this run's label — not the newest labeled entry anywhere in the
    history, which a host switch could misattribute.
    """
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        key = host_key_of(entry)
        return key if any(key) else None
    return None


def host_label_clause(history):
    """The host suffix for this run's messages: ' on <platform> (pyX)'.

    Empty when the run carries no host label (nothing to name).

    A stuck alert is an instruction to one machine; with more than one
    host in the history it must say WHICH one, or the operator has to
    guess before checking whether their fix was deployed there.
    """
    key = latest_host_key(history)
    if not key:
        return ""
    platform, py = key
    clause = f" on {platform}" if platform else " on an unlabeled host"
    return clause + (f" (py{py})" if py else "")


def route_of(lines):
    """The operator action a set of posted messages routes to.

    One classifier for every surface that shows a drift verdict — the
    /benchmark post, the /perfhistory view and the routing simulation —
    so they cannot disagree about what the operator should do:
      'fix'   a narrow regression: find and fix that read path;
      'rerun' an isolated dip: rerun before changing anything;
      'host'  a host-wide slide: rule the machine out first;
      'none'  nothing to act on.

    `CONTEXT_MARK` lines describe a DIFFERENT machine's still-open
    alert; they are reporting, not a verdict on this run, so they never
    set the action (a clean run on host B must route 'none' even while
    host A's regression is still open).
    """
    text = " ".join(ln for ln in lines
                    if not ln.startswith(CONTEXT_MARK))
    if "REGRESSION" in text:
        return "fix"
    if "host noise first" in text:
        return "rerun"
    if "host-wide" in text:
        return "host"
    return "none"


def phase_slide(ratios, baseline, floor=0.25):
    """(slid, measured) for THIS run: phases below the floor, phases with a bar.

    Breadth is the discriminator a single phase's own history cannot
    give: a host burst lands on EVERY phase at once (same machine, same
    CPU/IO weather), while a code change lives in ONE phase's read
    path. `slid` keeps `ratios` order so the message reads like the
    bench output; `measured` counts the phases that had a baseline to
    compare against.
    """
    recorded = (baseline or {}).get("phases") or {}
    slid, measured = [], 0
    for phase, ratio in (ratios or {}).items():
        bar = recorded.get(phase)
        if not bar or not isinstance(ratio, (int, float)):
            continue
        measured += 1
        if ratio < bar * floor:
            slid.append(phase)
    return slid, measured


def _shared_slide_warning(slid, ratios, recorded, floor, history):
    """One line for a run where EVERY measured phase slid together.

    A shared slide is evidence about the MACHINE (or a path every phase
    goes through), so it must not be reported as N independent phase
    regressions — that sends the operator hunting for N code changes
    while the real cause is host-wide. Persistence still separates the
    two host-wide cases: a transient burst needs a rerun on a quiet
    box, a sustained shared slide needs the host ruled out (idle
    rerun, host metadata) before the shared path is blamed. Persistence
    is counted in THIS run's host's own entries: an interleaved
    healthy host must not dilute a sustained slide into "rerun", and a
    brand-new host's two runs must not claim one.
    """
    this_run = ", ".join(f"{p} {ratios[p]:.1f}x" for p in slid)
    bars = ", ".join(f"{p} {recorded[p]:.1f}x" for p in slid)
    host_clause = host_label_clause(history)
    host_key = latest_host_key(history)
    # Persistence is a claim about THIS machine's record. Pooled, a
    # healthy second host's entries halve the count and a sustained
    # shared slide on an interleaved machine would be called transient
    # ("rerun") when its own runs say otherwise.
    low, seen = max(
        (phase_evidence(p, history, recorded[p] * floor, host_key=host_key)
         for p in slid),
        key=lambda evidence: (evidence[0], evidence[1]),
    )
    scope = "that host's last" if host_key else "the last"
    if sustained_regression(low, seen):
        return (
            f"⚠️ all {len(slid)} measured phases have slid together "
            f"({this_run} vs committed {bars}; low in {low} of {scope} "
            f"{seen} recorded runs){host_clause} — a shared slide is "
            "host-wide: either "
            "this MACHINE (CPU/IO contention, a slower disk, a busy "
            "neighbour) or a SHARED path every phase goes through. Rule "
            "the host out first — rerun /benchmark when the box is quiet "
            "and compare hosts in /perfhistory — and only if it survives "
            "a host change look for shared code (re-recording now would "
            "hide it in the baseline)."
        )
    return (
        f"⚠️ all {len(slid)} measured phases slid together this run "
        f"({this_run} vs committed {bars}; low in {low} of {scope} "
        f"{seen} recorded runs){host_clause} — a shared slide is "
        "host-wide: "
        "the HOST (CPU/IO contention or another process), not one read "
        "path. Rerun /benchmark when the box is quiet before touching "
        "the code or the baseline."
    )


def drift_warnings(ratios, baseline, floor=0.25, history=None):
    """Warnings for phases far off committed, ROUTED so a real
    regression is fixed, a host event is not, and nothing is re-recorded.

    `floor` is a fraction of the committed ratio — 0.25 means a phase
    running at under a quarter of its committed speedup is drift. That
    is stricter than the CI guard's collapse floor (0.10) on purpose:
    the drift line posts proactively on every /benchmark, so it should
    fire before CI would fail, without being so eager that ordinary
    hardware differences nag on every call.

    A flagged phase is routed by TWO pieces of evidence:

      1. BREADTH (this run, `phase_slide`): every measured phase below
         the floor at once is a HOST-wide slide — a host burst, CPU/IO
         contention, a busy neighbour — and is never evidence for one
         phase's read path; so it gets ONE host-wide line, not N phase
         warnings. One phase sliding while the others sit at baseline
         points INTO that phase's read path. A single-phase run has no
         breadth evidence and makes no breadth claim.
      2. PERSISTENCE (`history`, see `sustained_regression`): low at the
         fit's own bar of recent runs ON THIS HOST means a real change,
         not a hiccup (per host, so a second machine's healthy entries
         cannot dilute it into "rerun").    Routes: narrow + sustained -> REGRESSION: fix that phase's read path.
    Wide -> host-wide: rule the host out first (idle rerun, host
    metadata in /perfhistory), then a shared path if it survives a host
    change. Narrow + isolated -> suspect host noise first and rerun.
    Every line names the machine it measured (`on <platform> (pyX)`)
    when the run carried host metadata: with two hosts in the history,
    "this run" is ambiguous, and a fix instruction must not be applied
    to the wrong box.

    A phase low for longer than the whole window (STUCK_MIN_STREAK) in
    the run's OWN HOST's runs is not a new dip: the fix route ESCALATES
    to a stuck alert that names that host — an applied fix is not
    holding — instead of repeating "find and fix". The count is
    per-host so an interleaved healthy host cannot keep resetting it.
    No branch ever recommends `--record`; re-recording is a deliberate,
    review-visible act for an INTENDED change (see CONTRIBUTING), not a
    remedy for a warning.

    Returns a list of human-readable strings (empty = clean, and a
    missing baseline returns the empty list too — nothing to drift
    against).
    """
    if not baseline or not ratios:
        return []
    slid, measured = phase_slide(ratios, baseline, floor)
    if not slid:
        return []
    recorded = baseline.get("phases") or {}
    if measured >= 2 and len(slid) == measured:
        return [_shared_slide_warning(slid, ratios, recorded, floor, history)]

    warnings = []
    host_key = latest_host_key(history)
    host_clause = host_label_clause(history)
    scope = "that host's last" if host_key else "the last"
    for phase in slid:
        ratio = ratios[phase]
        bar = recorded[phase]
        threshold = bar * floor
        # Persistence belongs to THIS machine: pooled, a healthy second
        # host halves the count and a sustained one-phase regression on
        # an interleaved host reads as "rerun" while the trend line
        # (already per host) calls it a REGRESSION.
        low, seen = phase_evidence(phase, history, threshold,
                                   host_key=host_key)
        # Saturation is counted in THIS HOST's own runs. Pooled, an
        # interleaved healthy host resets the count every time it logs,
        # so a machine that has been low for 24 of its own runs would
        # never escalate — the alert would repeat "find and fix" on the
        # one host that has already had a fix applied.
        stuck = low_streak(phase, history, threshold, host_key)
        # The cross-phase clause exists only when there WAS something to
        # compare: a one-phase run must not claim the others held.
        narrow = ""
        if measured >= 2:
            others = measured - len(slid)
            narrow = (f"only this phase slid this run (the other {others} "
                      f"measured phase{'s' if others != 1 else ''} stayed "
                      "at baseline, so the machine is not uniformly slow); ")
        if stuck > TREND_WINDOW:
            # Saturated evidence: the same "find and fix" every run
            # adds nothing, and if a fix was applied it is not holding.
            # Say THAT instead of repeating the first alert.
            warnings.append(
                f"⚠️ phase '{phase}' runs at {ratio:.1f}x vs the "
                f"committed baseline {bar:.1f}x{host_clause} — {narrow}it "
                f"has now been low for {stuck} consecutive runs of its "
                f"own, so its whole {TREND_WINDOW}-run window is "
                "saturated: this is not a new dip but the same unresolved "
                "REGRESSION, and a fix already applied is not holding (or "
                "none was applied yet). Check the change is deployed and "
                "is the right cause; re-recording now would hide it in "
                "the baseline."
            )
        elif sustained_regression(low, seen):
            warnings.append(
                f"⚠️ phase '{phase}' runs at {ratio:.1f}x vs the committed "
                f"baseline {bar:.1f}x{host_clause} — {narrow}it is that "
                f"slow in {low} "
                f"of {scope} {seen} recorded runs, so treat it as a "
                "REGRESSION: find and fix the change in this phase's "
                "read path (re-recording now would hide it in the "
                "baseline)."
            )
        else:
            warnings.append(
                f"⚠️ phase '{phase}' runs at {ratio:.1f}x vs the committed "
                f"baseline {bar:.1f}x{host_clause} this run — "
                f"{narrow}the recent record "
                "is not consistently this slow, so suspect host noise "
                "first: rerun /benchmark to confirm before touching the "
                "baseline or the code."
            )
    return warnings


def record_baseline(phases, overall):
    """Commit the current per-phase ratios as the guarded baseline."""
    payload = {
        "recorded_via": "scripts/bench_bulk_reads.py --record",
        "phases": {k: round(v, 2) for k, v in phases.items()},
        "end_to_end": round(overall, 2),
    }
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print(f"baseline written: {os.path.normpath(BASELINE_PATH)}")
    return payload


# ---------------------------------------------------------------------------
# Perf history: dated ratios per run, written every run (fail-soft), so
# drift can be TRENDed across runs instead of judged one alert at a time.
# ---------------------------------------------------------------------------

def host_metadata():
    """{python, platform} — the machine that produced the ratios.

    Ratios are self-relative (old vs bulk on the SAME machine), but the
    ABSOLUTE ratio a machine reaches depends on its CPU/storage, so a
    "trend" of falling phases can simply be a host switch. Recording
    the host per run is what lets the trend check and /perfhistory say
    "host change" instead of "code regression".
    """
    try:
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
    except Exception:  # exotic/embedded platforms — never block the run
        return None


def history_entry(ratios, overall, now_iso, recorded=False, host=None):
    """One JSONL row: {ts, phases, overall, recorded[, host]}.

    `host` is omitted when unknown so pre-host-metadata entries and
    metadata-less writers stay schema-compatible (everything reading
    the history file treats a missing host as "unknown").
    """
    entry = {
        "ts": now_iso,
        "phases": {k: round(v, 2) for k, v in (ratios or {}).items()},
        "overall": round(overall, 2) if overall else None,
        "recorded": bool(recorded),
    }
    if host:
        entry["host"] = {
            k: str(v)[:48] for k, v in host.items() if v
        } or None
        if entry["host"] is None:
            del entry["host"]
    return entry


def load_history(path=None):
    """All parsed entries (oldest first), [] when absent/unreadable.

    Corrupt lines are skipped, never fatal - a half-written JSONL from a
    killed process must not take the trend check down with it.
    """
    try:
        # Default to the EFFECTIVE path, so a `PERF_HISTORY_PATH`
        # redirect (CI, a test suite, an operator keeping the reviewed
        # file clean) is read where the bench WRITES it — otherwise the
        # trend check and /perfhistory would judge a different file.
        with open(path or effective_history_path(), encoding="utf-8") as fh:
            out = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
    except OSError:
        return []


_LEGACY_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "tests", "perf_history.jsonl"
)


def migrate_legacy_history():
    """One-time copy of the pre-data/ log sitting under tests/.

    The history file is runtime evidence (measurements taken on real
    hosts), not a test fixture — it lives in data/ now. Hosts upgrading
    from the old layout keep their trend history: the legacy file is
    COPIED once when the default location is empty, never deleted and
    never touched when PERF_HISTORY_PATH redirects (a custom location is
    managed by its operator; migrating into it would be surprising).
    """
    target = os.environ.get("PERF_HISTORY_PATH") or PERF_HISTORY_PATH
    if target != PERF_HISTORY_PATH:
        return False
    try:
        legacy_exists = os.path.isfile(_LEGACY_HISTORY_PATH)
        target_exists = os.path.isfile(PERF_HISTORY_PATH)
        if legacy_exists and not target_exists:
            os.makedirs(os.path.dirname(PERF_HISTORY_PATH), exist_ok=True)
            shutil.copyfile(_LEGACY_HISTORY_PATH, PERF_HISTORY_PATH)
            return True
    except OSError:
        pass  # fail-soft: a missing/unwritable data/ just starts fresh
    return False


def effective_history_path():
    """Where this run's history entry goes.

    PERF_HISTORY_PATH (env) overrides the default — a way for CI/tests
    or an operator to keep the local repo's committed history untouched
    (the test suite runs the bench for real; without the override every
    pytest run would append to the file under review).
    """
    return os.environ.get("PERF_HISTORY_PATH") or PERF_HISTORY_PATH


def append_history(entry, path=None):
    """Persist one entry. Returns True on success, False fail-soft."""
    try:
        rows = load_history(path) + [entry]
        if len(rows) > HISTORY_KEEP:
            rows = rows[-HISTORY_TRIM_TO:]
        with open(path or effective_history_path(), "w",
                  encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def default_trend_min_drifts(slice_len):
    """ceil(n * TREND_MIN_DRIFTS_FRACTION), never below 2 — scales with
    the slice it is applied to.

    Applied per host slice: 3 entries -> 2 (the floor, 67%), 5 -> 3
    (the old 5-window policy's slice), 10 -> 6 (the fitted default);
    thin slices pay a higher share because the floor-2 guard holds.
    """
    return max(2, math.ceil(slice_len * TREND_MIN_DRIFTS_FRACTION))


def drift_trend_lines(ratios, baseline, history, floor=0.25,
                      window=TREND_WINDOW, min_drifts=None):
    """PER-HOST trend lines for phases that drift repeatedly on one host.

    A single run below the committed baseline is reported by
    `drift_warnings` (could be transient host noise). This function is
    the memory, evaluated host-by-host: each host speaks ONLY from its
    OWN last `window` runs (`host_window`; unlabeled runs group as an
    unlabeled slice), and it needs >= HOST_MIN_SLICE of them and
    >= TREND_MIN_DRIFTS_FRACTION below `baseline * floor` (threshold =
    ceil(share x slice), floor 2; explicit `min_drifts` overrides that
    threshold verbatim for callers who know better). A host only has to
    have run inside the recent window to be reported at all. The window
    is per MACHINE, so an interleaved host gets the same 10-run evidence
    a single machine gets, and a host that has been idle keeps the
    record it built (it comes back to a full window of its own runs, not
    an empty slice). Consequences:
      * a slow NEW host cannot FAKE a trend by pooling its low entries
        with another host's healthy runs;
      * a host switch cannot MASK a regression — a regressed host's own
        dips fire without other hosts' healthy entries diluting the count.
    Every line names the host it measured, so "host change" can no
    longer hide inside a mixed-window warning.

    A line is a claim about the run that carries it, so only the NEWEST
    entry's host speaks a verdict (the action `route_of` classifies).
    Another host's still-open alert is reported once as a
    `CONTEXT_MARK` line instead: after a switch, the machine that just
    measured clean must not re-post the previous machine's regression
    as its own action, and that regression must not vanish either.

    Breadth comes from the LATEST run (`phase_slide`): when every
    measured phase slid together, the qualifying phases get ONE
    host-wide line instead of N per-phase "fix this read path" lines —
    the operator must not hunt for N code changes on a machine (or a
    shared path) that is simply slow. One phase drifting while the
    others hold keeps the per-phase REGRESSION line, now saying so.
    A host's own window saturated for longer than its length escalates
    the line too (see `STUCK_MIN_STREAK`): the same unresolved
    regression, with an applied fix that is not holding. The count is
    per-host, so an interleaved healthy host cannot reset it — the
    stuck line names the machine it is about.

    Returns human-readable strings ([] = no repeat drift, or nothing
    to compare against - missing baseline / history are not trends).
    """
    if not baseline or not ratios or not history:
        return []
    recorded = (baseline.get("phases") or {})
    tail = [e for e in history[-window:]
            if isinstance(e, dict) and isinstance(e.get("phases"), dict)]
    # Hosts active in the recent window are the candidates (a host that
    # has not run since then is not news), but each speaks from its OWN
    # last `window` runs: pooling the shared window would give an
    # interleaved host half a window to prove itself and would evict a
    # host's record after `window` runs on another machine.
    recent: list = []
    for entry in tail:
        key = host_key_of(entry)
        if key not in recent:
            recent.append(key)
    # {(phase, host_key): (host_key, [values])} — host_key = (platform, python)
    slices: dict = {}
    for key in recent:
        for entry in host_window(history, key, window):
            for phase in ratios:
                v = entry["phases"].get(phase)
                if isinstance(v, (int, float)):
                    slices.setdefault(phase, {}).setdefault(key, []).append(v)

    # Breadth of the LATEST run decides how repeat drift is reported.
    slid, measured = phase_slide(ratios, baseline, floor)
    broad = measured >= 2 and len(slid) == measured
    one_phase = measured >= 2 and len(slid) == 1

    qualified: dict = {}  # host_key -> [(phase, drifts, slice_len)]
    for phase, hosts in slices.items():
        bar = recorded.get(phase)
        if not bar:
            continue
        for host_key, vals in hosts.items():
            if len(vals) < HOST_MIN_SLICE:
                continue  # thin slice: not enough of this host's own runs
            threshold = (min_drifts if min_drifts is not None
                         else default_trend_min_drifts(len(vals)))
            drifts = sum(1 for v in vals if v < bar * floor)
            if drifts < threshold:
                continue
            qualified.setdefault(host_key, []).append(
                (phase, drifts, len(vals)))

    lines, context = [], []
    run_host = latest_host_key(history)
    for (label, py), hits in qualified.items():
        if label:
            clause = f" on {label}" + (f" (py{py})" if py else "")
        else:
            clause = (" on an unlabeled run (host unknown — rerun the "
                      "bench to tag it)")
        # Only the machine that just ran gets to say what to do. With
        # no label on the newest entry, the unlabeled slice is the
        # run's own (and every named host is somebody else).
        mine = (((label or None, py or None) == run_host) if run_host
                else not label and not py)
        if not mine:
            context.append(_other_host_line((label, py), hits, run_host))
            continue
        if broad and len(hits) >= 2:
            detail = ", ".join(f"{p} {d} of {n}" for p, d, n in hits)
            stuck_max = max(low_streak(p, history, recorded[p] * floor,
                                       (label, py)) for p, _, _ in hits)
            stuck_tail = ""
            if stuck_max > TREND_WINDOW:
                stuck_tail = (f" The window has been saturated for "
                              f"{stuck_max} consecutive runs — a shared "
                              "fix already applied is not holding.")
            lines.append(
                f"📈 all {len(hits)} phases have drifted together{clause} "
                f"({detail} of their last recorded runs) — a shared slide "
                "is host-wide: check the MACHINE first (idle rerun, host "
                "metadata in /perfhistory) and only then a shared path "
                "they all use; this is not one phase's regression "
                "(re-recording now would hide it in the baseline)."
                + stuck_tail
            )
            continue
        for phase, drifts, n in hits:
            narrow = ""
            if one_phase:
                others = measured - 1
                narrow = ("; only this phase is sliding in this run (the "
                          f"other {others} stayed at baseline), so this "
                          "is one read path, not the host")
            stuck = low_streak(phase, history, recorded[phase] * floor,
                               (label, py))
            # Saturation belongs to the HOST's own memory, not to the
            # shared window: with two hosts interleaved, only ~half the
            # window's entries are this host's, so requiring n >= the
            # whole window made escalation impossible for exactly the
            # host that has been low longest. `stuck > TREND_WINDOW`
            # already implies its last window of OWN runs is all low.
            if drifts >= n and stuck > TREND_WINDOW:
                lines.append(
                    f"📈 phase '{phase}' has drifted in all {n} of its "
                    f"last recorded runs{clause} — {stuck} consecutive "
                    f"lows: its own {TREND_WINDOW}-run window is "
                    "saturated, so this is an unresolved REGRESSION, not "
                    "repeat drift: a fix already applied is not holding — "
                    "check the change is deployed and is the right cause "
                    "(re-recording now would hide it in the baseline)."
                )
                continue
            lines.append(
                f"📈 phase '{phase}' has drifted in {drifts} of its last "
                f"{n} recorded runs{clause} — repeat drift is a "
                "REGRESSION signal: find and fix the change in this "
                f"phase's read path{narrow} (re-recording now would hide "
                "it in the baseline)."
            )
    return lines + context


def _other_host_line(key, hits, run_host):
    """One CONTEXT line for a host that is NOT the run's own machine.

    The alert is still open there and the operator should know, but it
    is not evidence about this run: it is phrased as a separate alert,
    marked so `route_of` ignores it, and it names both machines so
    nobody applies the fix to the box in front of them.
    """
    label, py = key
    who = ((f"{label}" + (f" (py{py})" if py else "")) if label
           else "an unlabeled host")
    here = ((f"{run_host[0]}" + (f" (py{run_host[1]})" if run_host[1] else ""))
            if run_host else "an unlabeled host")
    detail = ", ".join(f"'{phase}' {drifts} of {n}"
                       for phase, drifts, n in hits)
    return (
        f"{CONTEXT_MARK} separate alert, NOT this run's verdict: {who} "
        f"is still low ({detail} of its last recorded runs). This run "
        f"measured on {here}; that alert closes only with that machine's "
        "own healthy runs, so it is not evidence about this one."
    )


# ---------------------------------------------------------------------------
# The read-only history view: /perfhistory renders THIS, never re-running
# the bench. Pure string math like everything else above.
# ---------------------------------------------------------------------------

ROUTE_PHRASE = {
    "fix": "fix the regressed read path",
    "host": "rule the host out (host-wide slide)",
    "rerun": "rerun, suspect host noise",
    "none": "none",
}


def _key_label(key):
    """The one-line host label for tables: 'Linux/py3.12.10', '?/py?'.

    platform.platform() is long ("Windows-11-10.0.26100-SP0") and would
    blow up a table width; the first segment (OS name) is what a host
    switch LOOKS like in a ratio column, and the python version pins
    the usual suspects.
    """
    plat, py = key
    return f"{(plat or '?').split('-')[0]}/py{py or '?'}"


def _host_evidence(ratios, upto, baseline, floor, host_key):
    """The one-line 'why' for a machine's verdict: phase and its own count.

    A verdict says what to do; an operator reading a table of machines
    needs the measurement behind it without scrolling to that host's
    lines — which phase is implicated and how many of its OWN recent
    runs were low (the same per-host window the verdict used). Breadth
    is kept: every measured phase low at once is reported as such,
    because that is the host-wide signature that points at the machine
    rather than one read path.
    """
    recorded = (baseline or {}).get("phases") or {}
    counts = []
    for phase, bar in recorded.items():
        if not isinstance((ratios or {}).get(phase), (int, float)):
            continue
        low, seen = phase_evidence(phase, upto, bar * floor,
                                   host_key=host_key)
        if seen:
            counts.append((phase, low, seen))
    if not counts:
        return "no comparable phase"
    counts.sort(key=lambda c: (-c[1] / c[2], -c[1], c[0]))
    slid, measured = phase_slide(ratios, baseline, floor)
    if len(slid) >= 2 and len(slid) == measured:
        phase, low, seen = counts[0]
        return f"all {len(slid)} phases low (worst {phase} {low}/{seen})"
    phase, low, seen = counts[0]
    if low == 0:
        return "nothing below its floor"
    return f"{phase} {low}/{seen} low"


def host_status_rows(history, baseline, floor=0.25):
    """One row per machine: its last run, the verdict IT last had, why.

    The whole point of the view is the machine, not the file: a host
    that has not run since the runs moved elsewhere has no row in the
    recent-runs table and no verdict (its lines are gone from the
    window), so its alert would look resolved. Each row replays the
    history up to THAT host's last run through the same two message
    functions /benchmark posts, which is the verdict that machine got
    when it last measured, plus the evidence behind it (`evidence`: the
    implicated phase and how many of the host's OWN recent runs were
    low, or the host-wide breadth).

    Returns [{key, ts, runs, route, stuck, evidence}] ordered by last
    run (the machine quiet longest first), so the rows read like the
    table.
    """
    count: dict = {}
    last_at: dict = {}
    for i, entry in enumerate(history or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("phases"),
                                                        dict):
            continue
        key = host_key_of(entry)
        count[key] = count.get(key, 0) + 1
        last_at[key] = i
    rows = []
    for key, last in last_at.items():
        upto = history[:last + 1]
        ratios = upto[-1]["phases"]
        lines = drift_warnings(ratios, baseline, floor=floor, history=upto)
        lines += drift_trend_lines(ratios, baseline, upto, floor=floor)
        route = route_of(lines)
        rows.append({
            "key": key, "ts": upto[-1].get("ts"), "runs": count[key],
            "route": route,
            # the same saturation signal the latest verdict carries
            "stuck": route == "fix" and any("not holding" in ln
                                            for ln in lines),
            "evidence": _host_evidence(ratios, upto, baseline, floor, key),
        })
    rows.sort(key=lambda r: str(r["ts"] or ""))
    return rows


def _host_table(rows):
    """The per-machine table text (host, last run, runs, evidence, verdict).

    Evidence sits before the verdict so the row reads left to right:
    which machine, when it last measured, how much of its record there
    is, what that record shows, and what to do about it.
    """
    head = ["host", "last run (UTC)", "runs", "evidence (its own window)",
            "verdict"]
    body = []
    for row in rows:
        phrase = ROUTE_PHRASE[row["route"]]
        if row["stuck"]:
            phrase += " (alert stuck)"
        body.append([
            _key_label(row["key"]),
            str(row["ts"] or "?")[5:16].replace("T", " "),
            str(row["runs"]), row["evidence"], phrase,
        ])
    widths = [max(len(str(r[i])) for r in [head] + body)
              for i in range(len(head))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
            for r in [head] + body]


def host_status_text(history, baseline, floor=0.25):
    """The per-machine table as lines; [] when the log holds one machine.

    One renderer for every surface that shows it: `/perfhistory` embeds
    it in the read-only view, and `/benchmark` posts it under the run so
    the alert channel carries the other machines' verdicts too instead
    of only the host that just measured. Keeping it here is what stops
    the two posts from describing the same file differently.
    """
    rows = host_status_rows(history, baseline, floor)
    if len(rows) < 2:
        return []
    caption = ("per machine (each host replayed up to its LAST run: a "
               "host that has not run recently keeps that verdict):")
    return [caption] + _host_table(rows)


def format_history(history, baseline, limit=8, floor=0.25):
    """A compact read-only view of the accumulated perf history.

    Shows up to the `limit` most recent runs (oldest first, so the trend
    reads left to right), one column per phase known to those runs, with
    a ⚠️ cell suffix when that run's phase fell below `baseline * floor`.
    With more than one machine in the log it then prints ONE ROW PER
    HOST (`host_status_rows`): that machine's last run and the verdict
    it last got, because a host that has not run since the runs moved
    elsewhere has no ⚠️ row and no line in the window, and its alert
    would otherwise read as resolved.
    Ends with the ROUTED ACTION for the latest logged run — the same
    ⚠️/📈 messages /benchmark posts, rebuilt from this file alone
    (`drift_warnings` + `drift_trend_lines`) and labelled with
    `route_of`, so the read-only view tells the operator what to DO
    (fix / rerun / rule out the host), not just what happened. Returns
    a plain string (the caller adds the Discord code fence); an empty
    history gets its own instruction line instead of a table.
    """
    history = [e for e in (history or [])
               if isinstance(e, dict) and e.get("phases")]
    if not history:
        return "No bench runs recorded yet — run /benchmark to start the history."

    shown = history[-max(1, limit):]
    chrono = sorted(shown, key=lambda e: str(e.get("ts") or ""))
    latest = chrono[-1]

    # phases in the latest run's order first, then any older-only phases
    phases = []
    for ph in latest["phases"]:
        phases.append(ph)
    for entry in chrono:
        for ph in entry["phases"]:
            if ph not in phases:
                phases.append(ph)

    recorded = (baseline or {}).get("phases") or {}

    def cell(entry, ph):
        v = entry["phases"].get(ph)
        if not isinstance(v, (int, float)):
            return "—"
        text = f"{v:.1f}x"
        bar = recorded.get(ph)
        if bar and v < bar * floor:
            text += "⚠️"
        return text

    def host_cell(entry):
        return _key_label(host_key_of(entry))

    head = ["run (UTC)", "host"] + phases
    rows = [[str(e.get("ts") or "?")[5:16].replace("T", " "), host_cell(e)]
            + [cell(e, ph) for ph in phases] for e in chrono]
    widths = [max(len(str(r[i])) for r in [head] + rows)
              for i in range(len(head))]
    table = ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
             for r in [head] + rows]

    span_note = f"{len(shown)} shown of {len(history)} logged"
    ts0 = str(chrono[0].get("ts") or "?")
    tsN = str(latest.get("ts") or "?")
    out = [
        f"runs: {span_note} ({ts0} … {tsN})",
        "latest: " + tsN,
    ]
    shown_hosts = sorted({
        ((e.get("host") or {}).get("platform") or "unknown").split("-")[0]
        for e in chrono
    })
    if len(shown_hosts) > 1:
        out.append(
            "hosts: " + ", ".join(shown_hosts)
            + " — ratios are self-relative per host; compare within a host"
        )
    if recorded:
        bars = ", ".join(f"{p} {v:.1f}x" for p, v in recorded.items())
        out.append(f"committed baseline: {bars}  (⚠️ = below {floor:.0%} of it)")
    out.append("")
    out.extend(table)

    out.append("")
    if not recorded:
        out.append("route for the latest logged run (same messages "
                   "/benchmark posts): none — no committed baseline to "
                   "compare against")
        return "\n".join(out)
    # One row per MACHINE. The runs table shows the recent window, so a
    # host that has not run since the runs moved elsewhere has no ⚠️ row
    # AND no line in the window — its alert would read as resolved. The
    # rows are the verdicts those machines last got, each replayed from
    # its own record; the current run's verdict is the line below them.
    host_lines = host_status_text(history, baseline, floor)
    if host_lines:
        out.extend(host_lines)
        out.append("")
    # Rebuild the action /benchmark would post for this run, from the
    # same two functions and the history as it stood at that run — the
    # read-only view must not invent a verdict it cannot derive.
    latest_pos = max(i for i, e in enumerate(history) if e is latest)
    upto = history[:latest_pos + 1]
    lines = drift_warnings(latest["phases"], baseline, floor=floor,
                           history=upto)
    lines += drift_trend_lines(latest["phases"], baseline, upto, floor=floor)
    route = route_of(lines)
    verdict = {
        "fix": "fix the regressed read path",
        "host": "rule the host out first (host-wide slide)",
        "rerun": "rerun, suspect host noise first",
        "none": "none — nothing below the floor and no repeat drift",
    }[route]
    if route == "fix":
        # A saturated window means the prescribed fix is not holding;
        # the verdict must say so instead of repeating the first alert.
        # Counted per HOST, like the messages it summarises: a pooled
        # count would let a second host hide the stuck machine here even
        # while the lines below it name it.
        host_key = latest_host_key(upto)
        stuck = max(
            (low_streak(phase, upto, bar * floor, host_key)
             for phase, bar in recorded.items()
             if isinstance(latest["phases"].get(phase), (int, float))),
            default=0,
        )
        if stuck > TREND_WINDOW:
            verdict += (f" — the alert is stuck: low for {stuck} consecutive "
                        "runs, so a fix already applied is not holding")
    out.append("route for the latest logged run (same messages "
               "/benchmark posts): " + verdict)
    out.extend("  " + ln for ln in lines)

    # A clean verdict over a table with ⚠️ marks needs explaining, or the
    # operator thinks the view contradicts itself.
    flagged_earlier = sum(
        1 for entry in chrono[:-1]
        if phase_slide(entry["phases"], baseline, floor)[0]
    )
    if route == "none" and flagged_earlier:
        noun = "run" if flagged_earlier == 1 else "runs"
        verb = "was" if flagged_earlier == 1 else "were"
        out.append("")
        out.append(f"note: {flagged_earlier} earlier {noun} in this view "
                   f"{verb} flagged (⚠️) but the latest one is clean — "
                   "those marks are history, not a current action.")

    # The latest run can be healthy while the window still holds lows:
    # that is the alert AGEING OUT after a fix, not a cancelled alert.
    # Say how many more healthy runs it takes (the slowest aging phase
    # decides, scoped to the latest run's host like the trend lines).
    latest_slid, _measured = phase_slide(latest["phases"], baseline, floor)
    if route != "none" and not latest_slid:
        host_key = host_key_of(latest)
        waits = []
        for phase, bar in recorded.items():
            if not isinstance(latest["phases"].get(phase), (int, float)):
                continue
            if latest["phases"][phase] < bar * floor:
                continue  # this phase is still low: no countdown
            vals = []
            for entry in host_window(upto, host_key):
                v = (entry.get("phases") or {}).get(phase)
                if isinstance(v, (int, float)):
                    vals.append(v)
            if len(vals) < HOST_MIN_SLICE:
                continue
            need = default_trend_min_drifts(len(vals))
            low = sum(1 for v in vals if v < bar * floor)
            if low >= need:
                waits.append(low - need + 1)
        if waits:
            remaining = max(waits)
            noun = "run" if remaining == 1 else "runs"
            verb = "clears" if remaining == 1 else "clear"
            out.append("")
            out.append(f"recovery: still alerting from the window memory "
                       f"while the phase is back above the floor — "
                       f"{remaining} more healthy {noun} {verb} it.")
    return "\n".join(out)


def main():
    record = "--record" in sys.argv
    db_path = build_store()
    ids = list(range(1, N_GAMES + 1))
    game_dicts = [{"id": gid, "chart_rank": 10, "charts_sort_id": "sort_0",
                   "charts_sort": "Sort 0"} for gid in ids]
    # warm-up + measured runs
    old_s = bulk_s = 0.0
    for _ in range(2):
        old_s = bench_old_path(ids, [dict(g) for g in game_dicts])
        bulk_s = bench_bulk_path(ids, [dict(g) for g in game_dicts])
    per_phase = bench_per_phase(ids, [dict(g) for g in game_dicts])
    print(f"database: {db_path}")
    print(f"games: {N_GAMES}, snapshots/game: {SNAPSHOTS_PER_GAME}")
    print(f"old (per-game conn+query) x2:   {old_s:.3f}s")
    print(f"bulk (one query per metric) x2: {bulk_s:.3f}s")
    for phase, ratio in per_phase.items():
        print(f"speedup[{phase}]: {ratio:.1f}x")
    if bulk_s > 0:
        overall = old_s / bulk_s
        print(f"speedup: {overall:.1f}x")
        if record:
            record_baseline(per_phase, overall)
        migrate_legacy_history()
        target = effective_history_path()
        logged = append_history(history_entry(
            per_phase, overall,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            recorded=record,
            host=host_metadata(),
        ), path=target)
        if logged:
            print(f"history: logged to {os.path.normpath(target)}")
        else:
            print("history: could not write the perf history file (fail-soft, trend skipped)")


if __name__ == "__main__":
    main()
