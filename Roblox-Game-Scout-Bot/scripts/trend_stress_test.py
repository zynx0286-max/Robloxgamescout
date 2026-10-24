"""Stress-test the drift TREND check with injected random noise.

Replays synthetic histories through the REAL trend functions
(``drift_trend_lines``) and counts how often each window/threshold
setting declares a "repeat drift" trend. The truth in every stream is:
NO regression — every dip is transient host noise (load spikes, cold
storage, CI neighbours), so any 📈 trend line is a FALSE FIRE by
construction. The false-fire side is then weighed against the costside: how
many regressed runs the same settings need to catch a real sustained
regression (detection latency). A setting is only better if it keeps
the former tiny without inflating the latter.

Noise models (healthy draws bootstrap-resample the committed history's
observed growth ratios with +/- ``--jitter``; the jitter is clamped so
a "healthy" draw can never reach the drift floor — the real corpus
never misses the floor, and simulated healthy runs must not either):
  * iid    — each run independently dips below the drift floor with
             some probability (memoryless: pure noise, no clustering).
  * ar1    — the clustered model of record: a latent unit-variance
             AR(1) process x_t = rho*x_{t-1} + sqrt(1-rho^2)*e_t is
             thresholded at the marginal dip rate's Gaussian quantile,
             so P(dip) is EXACTLY the level while dips arrive in
             persistent runs (measured: mean run ~1.3-1.5 runs at
             rho 0.5, ~2-2.3 at 0.8, max run 10-20 there). This is the autocorrelated
             shape a slow host/load epoch actually has; the old
             burst+idle model hand-shaped that distribution (uniform
             1-4 run spikes), AR(1) derives it from one persistence
             parameter. iid is AR(1) at rho = 0.
  * bursty — kept available (``--bursty-levels``) as the retired
             1-4-run spike model, off by default so the clustered
             bands in the matrix are AR(1).

Each false-fire trial starts from an empty history (a fresh host's
fresh history file): a fire within the first ``window`` steps lands
while the per-host slice is still thin and the threshold sits at its
floor-2 minimum — those are counted as "thin" fires, reported
separately from "settled" (steady-state, full-slice) fires. The
``--warmup`` healthy prefix applies to the detection-latency probe
only, where a full slice is exactly the steady state to measure.

Seeds are derived per (model, level, setting, trial), so the numbers
are deterministic and only change when the noise model, the settings
under sweep, or the functions under test change.

Usage:
  py scripts/trend_stress_test.py                # full sweep (~1 min)
  py scripts/trend_stress_test.py --trials 25 --steps 60   # quick look
  py scripts/trend_stress_test.py --csv /tmp/trend.csv     # + CSV for
    scripts/render_stress_matrix.py, which renders the CONTRIBUTING
    matrix from the measured rows

It also checks ROUTING, not just detection, and routes by BREADTH: a
sustained regression in ONE phase (the others at baseline) arriving
with a concurrent host burst must send the operator to FIX that read
path, while ALL phases sliding together must send them to the host
(never to one read path, and never to re-record the regression into
the baseline); an isolated noise dip must send them to rerun. That
matrix is the S3 section; the negative control (no regression) sits
beside it. The S4 probe covers a regression that DEEPENS over time:
the route must escalate none -> rerun -> fix and turn to fix exactly on
that same fitted bar, never earlier and never to re-record. The S5
probe covers the other half of a usable alert: once the regression is
FIXED and healthy runs return, the alert must clear (5 healthy runs at
the 6-of-10 bar), the /perfhistory verdict must go back to "none", and
the alert must not come back afterwards. The S6 probe covers a PARTIAL
fix that later relapses: the route must age out during the stall, stay
quiet while the window is clear, then re-arm through rerun and reach
fix on the bar again for the second regression. The S7 probe covers the
STUCK alert: it may only escalate after a real consecutive-low streak
longer than the window (a marginal, flickering regression must not
trigger it early), and once escalated it must still age out with
healthy runs. The S8 probe puts that alert across TWO hosts: a host
that regresses on every one of its own runs, interleaved with a
healthy one, must be the host the stuck line NAMES, may only escalate
after its own (not pooled) streak passes the window, and must go quiet
once its own healthy runs return. The S9 probe covers a HOST-WIDE slide
on the same interleaved pair: every phase low on B's runs must route to
"rule the host out" (never one read path) and must claim persistence
from B's own record, while the identical slide from a brand-new host
(2 own runs) must stay transient — the old pooled rule called both
transient. The S10 probe moves the RUNS to another host mid-history: a
stuck alert on the first machine may be reported after the switch, but
it must never become the second machine's verdict (a clean run on the
new host has to route to nothing, and a slow new host earns its own
stuck alert at 11 of its OWN lows, never inherited from the old
machine's streak).

Reads the committed baseline + history only; writes nothing.
"""

import argparse
import csv
import math
import random
import statistics
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import bench_bulk_reads as bench

PHASE = "growth"
FLOOR = 0.25  # the drift floor fraction both check functions default to

# One fixed host: every run lands in the same per-host slice, which is
# exactly the "one machine, noisy runs" case the trend check is for.
HOST = {"python": "3.14.7", "platform": "Windows-11-10.0.26200-SP0"}

# Transient-burst shapes: (run lengths, weights). A burst of 4 at 10%
# frequency is deliberately present so the "4 consecutive dips" corner
# of the current default (thr 4 of 10) is exercised, not just 2-3 dips.
BURST_LEN = ([1, 2, 3, 4], [0.20, 0.50, 0.20, 0.10])

# The sweep: (window, min_drifts). min_drifts None = the production
# per-host scaling (default_trend_min_drifts); explicit ints are the
# counterfactuals a caller could configure (fixed thresholds that do
# NOT scale with the slice).
CONFIGS = [
    (3, None),   # scaled: thr 2 (floor of the scaling rule)
    (5, None),   # scaled: thr 3 — the pre-scaling default policy
    (5, 3),      # fixed thr 3 (strict 60%)
    (10, None),  # scaled: thr 6 — the CURRENT default (AR(1)-fitted)
    (10, 4),     # fixed thr 4 — the PREVIOUS default (40% share)
    (10, 2),     # fixed thr 2 — the "no scaling" counterfactual
    (20, 2),     # fixed thr 2 on a long window — the trigger-happy corner
]
CURRENT = (10, None)


def config_label(w, m):
    thr = m if m is not None else bench.default_trend_min_drifts(w)
    policy = "scaled" if m is None else "fixed"
    mark = "  <- CURRENT" if (w, m) == CURRENT else ""
    return thr, f"w{w} {policy} thr{thr}{mark}"


def load_corpus():
    """(baseline, observed healthy ratios) from the committed files.

    Falls back to the DEFAULT history path explicitly, so this works
    under the test suite's PERF_HISTORY_PATH override too."""
    baseline = bench.load_baseline()
    bar = (baseline or {}).get("phases", {}).get(PHASE)
    if not bar:
        sys.exit("no committed baseline growth value; record one first: "
                 "py scripts/bench_bulk_reads.py --record")
    observed = [
        e["phases"][PHASE]
        for e in bench.load_history(bench.PERF_HISTORY_PATH)
        if isinstance(e.get("phases"), dict)
        and isinstance(e["phases"].get(PHASE), (int, float))
    ]
    if not observed:
        sys.exit("no growth ratios in data/perf_history.jsonl; run one "
                 "bench partly to populate it")
    return baseline, observed


def healthy(observed, jitter, rng):
    return rng.choice(observed) * (1.0 + rng.uniform(-jitter, jitter))


def dip(rng, floor):
    # A transient dip lands 40-95% BELOW where the floor is: clearly
    # under the drift line (a near-miss is not the interesting case).
    return floor * rng.uniform(0.40, 0.95)


def ar1_stream(observed, jitter, floor, rng, steps, level, rho):
    """Latent Gaussian AR(1), thresholded to an exact marginal rate.

    x_t = rho*x_{t-1} + sqrt(1-rho^2)*e_t (stationary N(0,1)); a run
    dips when x_t > z(1 - level). So P(dip) = level by construction,
    and only the DEPENDENCE differs from the iid model: rho sets how
    long slow stretches last (a two-state approximation stays on a
    dip with probability ~rho at high rho). One parameter instead of
    a hand-tuned length distribution.
    """
    z = NormalDist().inv_cdf(1.0 - level)
    vals, x = [], rng.gauss(0.0, 1.0)
    while len(vals) < steps:
        x = rho * x + math.sqrt(max(0.0, 1.0 - rho * rho)) * rng.gauss(0.0, 1.0)
        vals.append(dip(rng, floor) if x > z
                    else healthy(observed, jitter, rng))
    return vals


def stream(observed, jitter, floor, rng, steps, model, level, rho=None):
    if model == "ar1":
        return ar1_stream(observed, jitter, floor, rng, steps,
                          level, 0.0 if rho is None else rho)
    vals, burst_left = [], 0
    while len(vals) < steps:
        if model == "iid":
            is_dip = rng.random() < level
        else:  # bursty
            if burst_left:
                burst_left -= 1
                is_dip = True
            elif rng.random() < level:
                burst_left = rng.choices(
                    BURST_LEN[0], weights=BURST_LEN[1])[0] - 1
                is_dip = True
            else:
                is_dip = False
        vals.append(dip(rng, floor) if is_dip
                    else healthy(observed, jitter, rng))
    return vals


HOST_B = {"python": "3.12.10", "platform": "Linux-6.8.0-1012-aws"}
# Two-host probe settings: the current default plus the pre-scaling
# (w5) policy — enough to answer "does per-host scoping change the
# picture at the settings an operator would actually have".
TWO_HOST_CONFIGS = [(5, None), (10, None)]
# Acting rates for the second host: well below the fitted 60% slice
# share, half of it, exactly at it, and constant.
ACTING_LEVELS = [0.15, 0.3, 0.6, 1.0]


def two_host_stream(observed, jitter, floor, rng, steps, k_b, p_b):
    """(which, value) pairs for a mixed history: host B runs every
    k_b-th step (dipping with prob p_b — its ACTING rate — else a
    healthy draw), host A takes every other step as a healthy draw.
    Either list feeding the probe: during warmup p_b=0 makes both
    hosts healthy."""
    out, i = [], 0
    while len(out) < steps:
        i += 1
        if i % k_b == 0:
            is_dip = rng.random() < p_b
            out.append(("B", dip(rng, floor) if is_dip
                        else healthy(observed, jitter, rng)))
        else:
            out.append(("A", healthy(observed, jitter, rng)))
    return out


def labeled_first_fire(w, m, baseline, warm, tail, pooled=False):
    """(fire_step, named_host) over (which, value) pairs.

    Pooled counterfactual: with `pooled=True` every entry's host label
    is stripped, so the current function lumps both hosts into ONE
    unlabeled slice — exactly the pre-host-metadata semantics, with
    the same threshold derivation. Names 'A'/'B' by platform
    substring in the fire line, else 'unlabeled'."""
    hist = []
    for i, (lab, v) in enumerate(warm + tail, 1):
        host = None if pooled else (HOST_B if lab == "B" else HOST)
        hist.append({"phases": {PHASE: round(v, 2)}, "host": host})
        lines = bench.drift_trend_lines({PHASE: v}, baseline, hist,
                                        window=w, min_drifts=m)
        if lines:
            if any("Windows" in ln for ln in lines):
                return i, "A"
            if any("Linux" in ln for ln in lines):
                return i, "B"
            return i, "unlabeled"
    return None, None


def _runs_elapsed(seq, upto_step, which):
    return sum(1 for lab, _ in seq[:upto_step] if lab == which)


def run_acting_b(baseline, observed, floor, jitter, args):
    """S1 — a slow ACTING second host interleaved with a healthy one.

    Who gets NAMED, and how fast the acting host is caught, per-host
    vs pooled. Fires naming healthy A are FALSE fires (misattribution);
    fires naming B (or unlabeled, pooled) are 'the acting host got
    caught'. Calibrated per (window, threshold) the thresholds are
    identical in both regimes (the pool slice = max slice), so the
    delta is pure host scoping."""
    print("\n=== S1 acting second host (B every 2nd step; A healthy) "
          "truth: B acting is REAL, so a fire naming B is correct; "
          "fires naming A are false ===")
    csv_out = []
    for p_b in ACTING_LEVELS:
        for w, m in TWO_HOST_CONFIGS:
            thr, label = config_label(w, m)
            buckets = {False: {"fired": 0, "A": 0, "B": 0, "uni": 0,
                               "lat": []},
                       True: {"fired": 0, "A": 0, "B": 0, "uni": 0,
                              "lat": []}}
            for t in range(args.trials):
                rng = random.Random(f"acting|{p_b}|w{w}|m{m}|{t}")
                warm = two_host_stream(observed, jitter, floor, rng,
                                       args.warmup, 2, 0.0)
                tail = two_host_stream(observed, jitter, floor, rng,
                                       args.steps, 2, p_b)
                seq = warm + tail
                warm_b = sum(1 for lab, _ in warm if lab == "B")
                for pooled in (False, True):
                    step, name = labeled_first_fire(w, m, baseline,
                                                    warm, tail, pooled)
                    if step is None:
                        continue
                    bk = buckets[pooled]
                    bk["fired"] += 1
                    # Latency in the ACTING host's own runs since the
                    # acting began: warmup B-runs are healthy history,
                    # not part of the detection delay.
                    bk["lat"].append(
                        _runs_elapsed(seq, step, "B") - warm_b)
                    if name == "A":
                        bk["A"] += 1
                    elif name == "B":
                        bk["B"] += 1
                    else:
                        bk["uni"] += 1
            for pooled, bk in buckets.items():
                regime = "pooled " if pooled else "per-host"
                med = (f"{statistics.median(bk['lat']):.1f}"
                       if bk["lat"] else "-")
                print(f"  B@{p_b:>4.0%} {label:<28}{regime:>9}"
                      f"{bk['fired']:>7}" + f"{bk['fired'] / args.trials:>8.1%}"
                      f"{bk['A']:>7}{bk['B']:>7}{bk['uni']:>7}"
                      f"{med:>9}")
                csv_out.append({
                    "kind": "twohost_acting",
                    "model": "twohost", "level": f"{p_b:g}",
                    "label": label, "window": w, "threshold": thr,
                    "regime": regime.strip(), "trials": args.trials,
                    "fired": bk["fired"],
                    "rate": f"{bk['fired'] / args.trials:.4f}",
                    "named_a": bk["A"], "named_b": bk["B"],
                    "unlabeled": bk["uni"],
                    "lat_host_med": (round(statistics.median(bk["lat"]), 1)
                                     if bk["lat"] else ""),
                })
    return csv_out


def run_mask_probe(baseline, observed, floor, jitter, args):
    """S2 — a REGRESSED host whose runs are only 1/p_a of the history.

    The mask question: does the healthy rest of the pool keep a real
    regression silent (pooled), and does per-host scoping still catch
    the regressed host from its own runs? Latency in total runs AND in
    the regressed host's own runs."""
    print("\n=== S2 regressed-host share probe (A dips EVERY time it "
          "runs; truth: real regression — zero fires here would be a "
          "missed one) ===")
    csv_out = []
    for p_a in (2, 4):
        share = 1 / p_a
        for w, m in TWO_HOST_CONFIGS:
            thr, label = config_label(w, m)
            buckets = {False: {"fired": 0, "tt": [], "aa": []},
                       True: {"fired": 0, "tt": [], "aa": []}}
            for t in range(args.trials):
                rng = random.Random(f"mask|{p_a}|w{w}|m{m}|{t}")
                warm = two_host_stream(observed, jitter, floor, rng,
                                       args.warmup, p_a, 0.0)
                tail, i = [], 0
                while len(tail) < args.steps:
                    i += 1
                    if i % p_a == 0:
                        tail.append(("A", floor * 0.5))
                    else:
                        tail.append(("B", healthy(observed, jitter, rng)))
                seq = warm + tail
                warm_a = sum(1 for lab, _ in warm if lab == "A")
                for pooled in (False, True):
                    step, name = labeled_first_fire(w, m, baseline,
                                                    warm, tail, pooled)
                    if step is None:
                        continue
                    bk = buckets[pooled]
                    bk["fired"] += 1
                    bk["tt"].append(step)
                    # Same convention as S1: A-runs since the regression
                    # started, excluding A's healthy warmup runs.
                    bk["aa"].append(
                        _runs_elapsed(seq, step, "A") - warm_a)
            for pooled, bk in buckets.items():
                regime = "pooled " if pooled else "per-host"
                tt = (f"{statistics.median(bk['tt']):.0f}"
                      if bk["tt"] else "-")
                aa = (f"{statistics.median(bk['aa']):.0f}"
                      if bk["aa"] else "-")
                print(f"  A@{share:>4.0%} {label:<28}{regime:>9}"
                      f"{bk['fired']:>7}" + f"{bk['fired'] / args.trials:>8.1%}"
                      f"{tt:>9}{'runs':>5}"
                      f"{aa:>7}{'A-runs':>7}")
                csv_out.append({
                    "kind": "twohost_mask", "model": "twohost",
                    "level": f"{share:.2f}", "label": label,
                    "window": w, "threshold": thr,
                    "regime": regime.strip(), "trials": args.trials,
                    "fired": bk["fired"],
                    "rate": f"{bk['fired'] / args.trials:.4f}",
                    "lat_total_med": (round(statistics.median(bk["tt"]), 0)
                                      if bk["tt"] else ""),
                    "lat_host_med": (round(statistics.median(bk["aa"]), 0)
                                     if bk["aa"] else ""),
                    "missed": args.trials - bk["fired"],
                })
    return csv_out


def first_fire(w, m, baseline, warm_vals, tail_vals):
    """Steps the system like the live driver: append, then evaluate.

    Returns the 1-based step index (over warm + tail) of the FIRST
    fire, or None if the setting stayed silent."""
    history = []
    for i, v in enumerate(warm_vals + tail_vals, 1):
        history.append({"phases": {PHASE: round(v, 2)}, "host": HOST})
        if bench.drift_trend_lines({PHASE: v}, baseline, history,
                                   window=w, min_drifts=m):
            return i
    return None


def false_fire_rows(baseline, observed, floor, jitter, args, model, level,
                    rho=None):
    """One sweep row per setting at this (model, level[, rho]).

    Each trial starts from an EMPTY history — exactly like a fresh
    host's fresh history file — so fires land in their real shape: at
    step <= window the history (and the per-host slice) is still thin
    and the threshold sits at its floor-2 minimum; after that the
    slice is full and the threshold is the steady-state one.
    """
    rows = []
    for w, m in CONFIGS:
        thr, label = config_label(w, m)
        fired = thin = settled = 0
        settled_steps = []
        for t in range(args.trials):
            # iid seeds intentionally unchanged from earlier runs; the
            # rho tag only appears for the AR(1) bands.
            tag = (f"stress|{model}" + ("" if rho is None else f"|r{rho}")
                   + f"|{level}|w{w}|m{m}|{t}")
            rng = random.Random(tag)
            noise = stream(observed, jitter, floor, rng, args.steps,
                           model, level, rho)
            hit = first_fire(w, m, baseline, [], noise)
            if hit is None:
                continue
            fired += 1
            if hit <= w:
                thin += 1
            else:
                settled += 1
                settled_steps.append(hit)
        rows.append({
            "w": w, "thr": thr, "label": label, "fired": fired,
            "thin": thin, "settled": settled,
            "rate": fired / args.trials,
            "mean": (statistics.mean(settled_steps)
                     if settled_steps else None),
        })
    return rows


def print_section(model, level, rows, trials, rho=None):
    name = "AR(1) cluster" if model == "ar1" else model
    tag = "" if rho is None else f", rho = {rho:g}"
    print(f"\n--- {name} noise, level = {level:g}{tag} "
          "(truth: no regression — every fire below is false)")
    print(f"  {'setting':<32}{'fired':>7}{'rate':>8}"
          f"{'thin':>7}{'settled':>9}{'mean step':>11}")
    any_settled = False
    for r in rows:
        mean = "" if r["mean"] is None else f"{r['mean']:.1f}"
        print(f"  {r['label']:<32}{r['fired']:>7}{r['rate']:>8.1%}"
              f"{r['thin']:>7}{r['settled']:>9}{mean:>11}")
        any_settled = any_settled or r["settled"] > 0
    if not any_settled:
        print(f"  (no steady-state fires in {trials} trials -> the 95% "
              f"upper bound on this rate is about 3/{trials})")


def latency_table(baseline, observed, floor, jitter, args):
    """Cost side: runs into a SUSTAINED regression until the fire.

    Prints the table AND returns its rows for --csv collection."""
    print(f"\n--- detection latency (every run at half the drift floor; "
          f"healthy warmup of {args.warmup}, cap {args.latency_cap})")
    print(f"  {'setting':<32}{'fired':>7}{'min':>6}{'median':>8}"
          f"{'max':>6}{'missed':>8}")
    rows = []
    for w, m in CONFIGS:
        thr, label = config_label(w, m)
        lat, missed = [], 0
        reg = floor * 0.5
        for t in range(args.latency_trials):
            rng = random.Random(f"latency|w{w}|m{m}|{t}")
            warm_vals = [healthy(observed, jitter, rng)
                         for _ in range(args.warmup)]
            tail = [reg] * args.latency_cap
            hit = first_fire(w, m, baseline, warm_vals, tail)
            if hit is None or hit - len(warm_vals) > args.latency_cap:
                missed += 1
            else:
                lat.append(hit - len(warm_vals))
        print(f"  {label:<32}{len(lat):>7}"
              + (f"{min(lat):>6}{statistics.median(lat):>8.1f}"
                 f"{max(lat):>6}" if lat
                 else f"{'-':>6}{'':>8}{'':>6}")
              + f"{missed:>8}")
        rows.append({
            "w": w, "thr": thr, "label": label, "fired": len(lat),
            "min": min(lat) if lat else "",
            "median": statistics.median(lat) if lat else "",
            "max": max(lat) if lat else "",
            "missed": missed,
        })
    return rows


# ---- threshold-policy re-fit under AR(1) -------------------------------
# The sweep above measures how the CURRENT policy behaves; this grid
# answers a different question: under the noise model of record (AR(1)),
# which (window, scaling fraction) is the best policy — including the
# production one? A policy is `window` + scaled threshold
# max(2, ceil(fraction * the host's own slice)), i.e. exactly the shape
# `default_trend_min_drifts` implements. Objective, stated up front so
# the pick cannot be cherry-picked after the fact: minimise the WORST
# false-fire rate across all AR(1) bands, subject to catching a
# sustained regression within --fit-max-latency runs (without the
# constraint, "never fire" wins every time).
FIT_WINDOWS = [5, 8, 10, 12, 15, 20]
FIT_FRACTIONS = [0.2, 0.25, 0.3, 0.4, 0.5, 0.6]


class _scaled_fraction:
    """Temporarily re-parameterize the production scaling rule.

    `default_trend_min_drifts` reads the module constant, so the fit
    evaluates candidate fractions through the SAME function the live
    check uses — a fit of a re-implementation would prove nothing.
    """

    def __init__(self, fraction):
        self.fraction = fraction

    def __enter__(self):
        self.orig = bench.TREND_MIN_DRIFTS_FRACTION
        bench.TREND_MIN_DRIFTS_FRACTION = self.fraction
        return self

    def __exit__(self, *exc):
        bench.TREND_MIN_DRIFTS_FRACTION = self.orig
        return False


def _fit_ff(baseline, observed, floor, jitter, args, w, level, rho):
    fired = 0
    for t in range(args.fit_trials):
        rng = random.Random(f"fit|r{rho}|{level}|w{w}|{t}")
        noise = ar1_stream(observed, jitter, floor, rng, args.fit_steps,
                           level, rho)
        if first_fire(w, None, baseline, [], noise) is not None:
            fired += 1
    return fired / args.fit_trials


def _fit_latency(baseline, observed, floor, jitter, args, w):
    """Runs into a sustained regression (half the floor) until fire."""
    lat, missed = [], 0
    reg = floor * 0.5
    for t in range(args.latency_trials):
        rng = random.Random(f"fitlat|w{w}|{t}")
        warm = [healthy(observed, jitter, rng) for _ in range(args.warmup)]
        hit = first_fire(w, None, baseline, warm, [reg] * args.latency_cap)
        if hit is None:
            missed += 1
        else:
            lat.append(hit - len(warm))
    return lat, missed


def refit_policy(baseline, observed, floor, jitter, args):
    """Grid-search (window, fraction) under AR(1) and pick by the
    pre-declared objective. Prints the full grid (so a reviewer can
    check the pick) plus the production policy's own score."""
    # The objective covers ORDINARY noise only (<= max(fit-levels)).
    # Heavier bands saturate every setting (measured: the 20%-marginal
    # AR(1) rows are ~100% for all policies) — that is the documented
    # "re-record, not investigate" signal, and including it would make
    # the worst-band score a constant.
    bands = [(rho, lv) for rho in args.ar1_rhos for lv in args.fit_levels]
    print("\n=== threshold re-fit under AR(1) (obj: min worst-band false "
          f"fire, s.t. median latency <= {args.fit_max_latency:g} runs; "
          f"{args.fit_trials} trials/band, {args.fit_steps} steps; "
          f"bands rho in {args.ar1_rhos} x level in {args.fit_levels}) ===")
    header = (f"  {'window':>6}{'frac':>6}{'thr@full':>9}"
              + "".join(f"{f'r{rho:g}/{lv:g}':>10}" for rho, lv in bands)
              + f"{'worst':>8}{'median lat':>11}{'missed':>7}")
    print(header)
    results = []
    for w in FIT_WINDOWS:
        for f in FIT_FRACTIONS:
            with _scaled_fraction(f):
                ffs = {band: _fit_ff(baseline, observed, floor, jitter,
                                     args, w, band[1], band[0])
                       for band in bands}
                lat, missed = _fit_latency(baseline, observed, floor,
                                           jitter, args, w)
            med = statistics.median(lat) if lat else None
            ok = bool(lat) and not missed and med <= args.fit_max_latency
            results.append({
                "w": w, "frac": f,
                "thr": max(2, math.ceil(f * w)),
                "bands": ffs, "worst": max(ffs.values()),
                "mean": statistics.mean(ffs.values()),
                "lat": lat, "median": med, "missed": missed, "ok": ok,
            })
    for r in results:
        mark = ""
        if (r["w"], r["frac"]) == (bench.TREND_WINDOW,
                                    bench.TREND_MIN_DRIFTS_FRACTION):
            mark = "  <- CURRENT"
        med = "-" if r["median"] is None else f"{r['median']:.1f}"
        print(f"  {r['w']:>6}{r['frac']:>6.2f}{r['thr']:>9}"
              + "".join(f"{r['bands'][b]:>9.0%}" for b in bands)
              + f"{r['worst']:>8.0%}{med:>11}"
              + f"{r['missed']:>7}{mark}")

    candidates = [r for r in results if r["ok"]] or results
    best = min(candidates,
               key=lambda r: (r["worst"], r["mean"], r["median"], r["w"]))
    current = next(r for r in results
                   if (r["w"], r["frac"]) == (bench.TREND_WINDOW,
                                               bench.TREND_MIN_DRIFTS_FRACTION))
    print(f"  -> fitted best: w{best['w']} frac {best['frac']:.2f} "
          f"(thr {best['thr']} of a full window), worst-band "
          f"{best['worst']:.0%}, median latency {best['median']:.0f}")
    print(f"  -> production:  w{current['w']} frac {current['frac']:.2f} "
          f"(thr {current['thr']} of a full window), worst-band "
          f"{current['worst']:.0%}, median latency "
          f"{current['median'] if current['median'] is not None else '-'}"
          + ("  [fitted == production]"
             if (best["w"], best["frac"]) == (current["w"], current["frac"])
             else "  [fitted != production]"))

    rows = []
    for r in results:
        for rho, lv in bands:
            rows.append({
                "kind": "refit", "model": "ar1", "rho": rho,
                "level": f"{lv:g}", "window": r["w"],
                "fraction": f"{r['frac']:.2f}", "threshold": r["thr"],
                "band": f"r{rho:g}/{lv:g}", "rate": f"{r['bands'][(rho, lv)]:.4f}",
                "worst_ff": f"{r['worst']:.4f}",
                "mean_ff": f"{r['mean']:.4f}",
                "fit_ok": "1" if r["ok"] else "0",
                "fit_best": "1" if r is best else "0",
                "current": "1" if (r["w"], r["frac"]) == (
                    bench.TREND_WINDOW,
                    bench.TREND_MIN_DRIFTS_FRACTION) else "",
                "lat_median": (f"{r['median']:.1f}"
                               if r["median"] is not None else ""),
                "lat_missed": r["missed"],
            })
    return rows


# ---- S3: operator ROUTING probe ----------------------------------------
# Detection is only half the job: once a phase is flagged, the message
# must route the operator to the right action, and WHICH phases slid is
# what separates the two causes:
#   * ONE phase slid, the others at baseline -> a code regression in
#     that read path: FIX it (never re-record it into the baseline);
#   * EVERY measured phase slid together -> host-wide: the MACHINE (a
#     host burst, CPU/IO contention) — or a path all of them share, but
#     never one read path. Rule the host out first.
# Re-recording a live regression writes it into the guarded baseline and
# CI forgets it, so no cell anywhere may recommend --record.
#
# The matrix is truth x noise mix x concurrent-burst timing. Truth is a
# sustained regression in ONE phase (0.20/0.12/0.06 x baseline, all
# below the 25% drift floor — a 0.4x slowdown is under the check's own
# threshold and out of scope), a HOST-WIDE sustained slide (every phase
# at 0.15 x baseline), or neither. The concurrent host burst drops
# EVERY phase for 3 onset runs — a host event cannot be local to one
# read path, which is exactly the cell that must not be called a
# phase regression.
ROUTING_REGRESSIONS = [None, 0.20, 0.12, 0.06]
ROUTING_SHARED = 0.15     # sustained host-wide slide: all phases together
ROUTING_NOISE = [
    ("none", 0.0, None),
    ("iid 5%", 0.05, None),
    ("iid 10%", 0.10, None),
    ("ar1 10% rho0.8", 0.10, 0.8),
]
ROUTING_BURST_LEN = 3     # concurrent host burst: short and at onset
ROUTING_TAIL = 24         # runs after onset per simulated timeline
ROUTING_WARMUP = 30       # healthy runs before onset (slice is full)


def _route_of(lines):
    """The operator action a set of messages routes to.

    Delegates to the production classifier, so the simulation measures
    the same contract /benchmark and /perfhistory use.
    """
    return bench.route_of(lines)


def _phase_bars(baseline):
    """{phase: committed ratio} for every phase the baseline records."""
    return {p: float(v) for p, v in (baseline.get("phases") or {}).items()
            if isinstance(v, (int, float)) and v}


def _healthy_run(bars, observed, jitter, rng):
    """One healthy multi-phase run: growth from the corpus, the rest
    around their own committed bars (breadth only needs them ABOVE the
    floor, which a healthy draw always is)."""
    return {p: (healthy(observed, jitter, rng) if p == PHASE
                else bar * (1.0 + rng.uniform(-jitter, jitter)))
            for p, bar in bars.items()}


def _routing_timeline(baseline, observed, floor, jitter, rng, reg, noise,
                      burst, shared=None):
    """(warm, tail) of per-phase ratio dicts for one scenario.

    Warmup is healthy (with the cell's noise mix). At onset, either ONE
    phase genuinely regresses (every subsequent run at `reg` x its own
    baseline), EVERY phase slides together (`shared` x baseline — a
    host-wide event), or nothing changes (negative control). `burst`
    adds ROUTING_BURST_LEN onset runs in which every phase drops: a
    host burst hits the machine, not one read path.
    """
    bars = _phase_bars(baseline)
    warm = [_healthy_run(bars, observed, jitter, rng)
            for _ in range(ROUTING_WARMUP)]
    label, level, rho = noise
    if rho is not None:
        growth = ar1_stream(observed, jitter, floor, rng, ROUTING_TAIL,
                            level, rho)
    elif level:
        growth = stream(observed, jitter, floor, rng, ROUTING_TAIL,
                        "iid", level)
    else:
        growth = [healthy(observed, jitter, rng)
                  for _ in range(ROUTING_TAIL)]
    tail = []
    for v in growth:
        run = {p: bar * (1.0 + rng.uniform(-jitter, jitter))
               for p, bar in bars.items()}
        run[PHASE] = v  # the phase whose noise mix the cell simulates
        tail.append(run)
    if reg is not None:
        for run in tail:
            run[PHASE] = bars[PHASE] * reg
    if shared is not None:
        for run in tail:
            for phase in run:
                run[phase] = bars[phase] * shared
    if burst:
        for run in tail[:ROUTING_BURST_LEN]:
            for phase in run:
                run[phase] = bars[phase] * 0.03
    return warm, tail


def routing_probe(baseline, observed, floor, jitter, args):
    """Run the routing matrix through the REAL message functions."""
    print("\n=== S3 operator routing (breadth: one phase slid = code "
          "regression, all phases slid = host-wide; + concurrent burst) ===")
    print(f"{'truth':<12}{'noise':<18}{'reg':>7}{'burst':>6}{'trend':>7}"
          f"{'fix':>7}{'host':>7}{'rerun':>7}{'none':>6}{'--record':>10}")
    scenarios = [("one phase", reg, None) for reg in ROUTING_REGRESSIONS]
    scenarios.append(("all phases", None, ROUTING_SHARED))
    rows = []
    for slide, reg, shared in scenarios:
        for label, level, rho in ROUTING_NOISE:
            for use_burst in (False, True):
                counts = {"fix": 0, "host": 0, "rerun": 0, "none": 0}
                trend_trials = 0
                record_advice = 0
                for t in range(args.routing_trials):
                    rng = random.Random(
                        f"route|{slide}|{label}|{reg}|{shared}|"
                        f"{use_burst}|{t}")
                    warm, tail = _routing_timeline(
                        baseline, observed, floor, jitter, rng, reg,
                        (label, level, rho), use_burst, shared)
                    history, last_route, saw_trend = [], "none", False
                    for run in warm + tail:
                        ratios = {p: round(v, 2) for p, v in run.items()}
                        history.append({"phases": ratios, "host": HOST})
                        lines = bench.drift_warnings(
                            ratios, baseline, history=history)
                        lines += bench.drift_trend_lines(
                            ratios, baseline, history)
                        record_advice += sum(
                            1 for ln in lines if "--record" in ln)
                        route = _route_of(lines)
                        if route != "none":
                            last_route = route
                        if any("📈" in ln for ln in lines):
                            saw_trend = True
                    counts[last_route] += 1
                    trend_trials += saw_trend
                n = args.routing_trials
                print(f"{slide:<12}{label:<18}"
                      f"{('-' if reg is None else f'{reg:.2f}'):>7}"
                      f"{('yes' if use_burst else 'no'):>6}"
                      f"{trend_trials / n:>7.0%}{counts['fix'] / n:>7.0%}"
                      f"{counts['host'] / n:>7.0%}"
                      f"{counts['rerun'] / n:>7.0%}"
                      f"{counts['none'] / n:>6.0%}"
                      f"{record_advice:>10}")
                rows.append({
                    "kind": "routing", "model": "routing",
                    "slide": slide,
                    "noise": label,
                    "regression": "" if reg is None else f"{reg:.2f}",
                    "burst": "yes" if use_burst else "no",
                    "trials": n,
                    "fix_rate": f"{counts['fix'] / n:.4f}",
                    "host_rate": f"{counts['host'] / n:.4f}",
                    "rerun_rate": f"{counts['rerun'] / n:.4f}",
                    "none_rate": f"{counts['none'] / n:.4f}",
                    "trend_rate": f"{trend_trials / n:.4f}",
                    "record_advice": record_advice,
                })
    return rows


# ---- S4: a regression that DEEPENS over time ---------------------------
# Real regressions rarely arrive as one clean step: a bad query plan, a
# growing table or a cache that stops hitting gets worse every run. The
# route must ESCALATE with the evidence — silent while the phase is still
# above the floor, "rerun, suspect host noise" while the dip is isolated,
# then "REGRESSION: fix it" exactly when the fitted evidence bar is met —
# and it must never recommend re-recording. This probe ramps ONE phase
# from its committed ratio down to 2% of it over 30 runs (geometric: slow
# first, unmistakable later), keeps the other phases healthy so the slide
# stays NARROW, and replays every step through the real messages.
DEEPENING_STEPS = 30
DEEPENING_END = 0.02
DEEPENING_JITTERS = [0.0, 0.10, 0.20]


def _deepening_timeline(baseline, observed, jitter, rng):
    """(warm, [(depth, run)]) for a one-phase regression that deepens.

    `depth` is this run's ratio as a fraction of the committed baseline;
    every run (and every other phase) carries the same +/-jitter, so a
    near-floor run can legitimately flip above it.
    """
    bars = _phase_bars(baseline)
    warm = [_healthy_run(bars, observed, jitter, rng)
            for _ in range(ROUTING_WARMUP)]
    step = math.log(DEEPENING_END) / (DEEPENING_STEPS - 1)
    tail = []
    for t in range(DEEPENING_STEPS):
        depth = math.exp(step * t)
        run = {p: bar * (1.0 + rng.uniform(-jitter, jitter))
               for p, bar in bars.items()}
        run[PHASE] = (bars[PHASE] * depth
                      * (1.0 + rng.uniform(-jitter, jitter)))
        tail.append((depth, run))
    return warm, tail


def deepening_probe(baseline, observed, floor, jitter, args):
    """S4: the deepening regression must escalate rerun -> fix ON the bar."""
    print("\n=== S4 escalation on a regression that deepens over time "
          f"(one phase 1.00x -> {DEEPENING_END:.2f}x baseline over "
          f"{DEEPENING_STEPS} runs; route: none -> rerun -> fix, turning "
          "on the fitted evidence bar, never '--record') ===")
    print(f"{'jitter':>7}{'fix@':>7}{'rerun run':>11}{'fix run':>9}"
          f"{'escal':>7}{'depth@fix':>11}{'on bar':>8}"
          f"{'premature':>11}{'--record':>10}")
    rows = []
    for jit in DEEPENING_JITTERS:
        reruns, fixes, escalations, depths = [], [], [], []
        on_bar = 0
        premature = 0
        record_advice = 0
        for t in range(args.routing_trials):
            rng = random.Random(f"deepen|{jit:g}|{t}")
            warm, tail = _deepening_timeline(baseline, observed, jit, rng)
            history, first_rerun, first_fix = [], None, None
            depth_at_fix = None
            met_at_fix = False
            for i, (depth, run) in enumerate(tail, start=len(warm)):
                ratios = {p: round(v, 2) for p, v in run.items()}
                history.append({"phases": ratios, "host": HOST})
                # `floor` is the ABSOLUTE drift floor (baseline x share).
                low, seen = bench.phase_evidence(PHASE, history, floor)
                met = bench.sustained_regression(low, seen)
                lines = bench.drift_warnings(ratios, baseline,
                                             history=history)
                lines += bench.drift_trend_lines(ratios, baseline, history)
                record_advice += sum(1 for ln in lines if "--record" in ln)
                route = _route_of(lines)
                if route == "fix":
                    if not met:
                        premature += 1
                    if first_fix is None:
                        first_fix = i - ROUTING_WARMUP
                        depth_at_fix = depth
                        met_at_fix = met
                elif route == "rerun" and first_fix is None:
                    if first_rerun is None:
                        first_rerun = i - ROUTING_WARMUP
            if first_fix is not None:
                fixes.append(first_fix)
                on_bar += 1 if met_at_fix else 0
                if depth_at_fix is not None:
                    depths.append(depth_at_fix)
                if first_rerun is not None:
                    reruns.append(first_rerun)
                    escalations.append(first_fix - first_rerun)
        n = args.routing_trials
        reach = len(fixes) / n
        rerun_med = (f"{statistics.median(reruns):.0f}" if reruns else "-")
        fix_med = f"{statistics.median(fixes):.0f}" if fixes else "-"
        escal_med = (f"{statistics.median(escalations):.0f}"
                     if escalations else "-")
        depth_med = (f"{statistics.median(depths):.0%}" if depths else "-")
        print(f"{jit:>7.2f}{reach:>7.0%}{rerun_med:>11}{fix_med:>9}"
              f"{escal_med:>7}{depth_med:>11}"
              f"{on_bar / n:>8.0%}{premature:>11}{record_advice:>10}")
        rows.append({
            "kind": "deepening", "model": "deepening",
            "jitter": f"{jit:.2f}", "trials": n,
            "reach_rate": f"{reach:.4f}",
            "rerun_med": rerun_med, "fix_med": fix_med,
            "escal_med": escal_med,
            "depth_med": (f"{statistics.median(depths):.4f}"
                          if depths else ""),
            "bar_rate": f"{on_bar / n:.4f}",
            "premature": premature,
            "record_advice": record_advice,
        })
    return rows


# ---- S5: RECOVERY after the regression is fixed -------------------------
# An alert that never stops is as useless as one that never fires. The
# 10-run window IS the memory: a regression long enough to saturate it
# stops alerting when fewer than 6 of the last 10 runs are low, i.e. on
# the 5th healthy run (HOST window - bar + 1). This probe measures that
# on the real messages and on the /perfhistory verdict, and checks the
# alert does not come back.
RECOVERY_REGRESSION = 0.12
RECOVERY_RUNS = 10          # healthy runs observed after the fix
RECOVERY_JITTERS = [0.0, 0.15]


def _recovery_timeline(baseline, observed, floor, jitter, rng):
    """(warm, regressed, recovered) runs for one fixed-regression story.

    The regression lasts ROUTING_TAIL runs (long enough to saturate the
    window); `recovered` is RECOVERY_RUNS healthy runs of the same
    phase, so the alert has to age out of the window rather than be
    cancelled by the fix itself.
    """
    warm, tail = _routing_timeline(
        baseline, observed, floor, jitter, rng, RECOVERY_REGRESSION,
        ("none", 0.0, None), False)
    bars = _phase_bars(baseline)
    recovered = [_healthy_run(bars, observed, jitter, rng)
                 for _ in range(RECOVERY_RUNS)]
    return warm, tail, recovered


def recovery_probe(baseline, observed, floor, jitter, args):
    """S5: after the regression is fixed, the alert must clear."""
    print("\n=== S5 recovery after the regression is fixed (the route must "
          "go back to 'none'; the 10-run window is the memory) ===")
    print(f"{'jitter':>7}{'cleared':>9}{'clear at':>10}{'slowest':>9}"
          f"{'up on 1st':>11}{'stayed clear':>14}{'view none':>11}"
          f"{'--record':>10}")
    rows = []
    for jit in RECOVERY_JITTERS:
        clears, first_up, stayed, view_none = [], 0, 0, 0
        record_advice = 0
        for t in range(args.routing_trials):
            rng = random.Random(f"recover|{jit:g}|{t}")
            warm, tail, recovered = _recovery_timeline(
                baseline, observed, floor, jit, rng)
            history, alerting = [], []
            for run in warm + tail + recovered:
                ratios = {p: round(v, 2) for p, v in run.items()}
                history.append({"phases": ratios, "host": HOST})
                lines = bench.drift_warnings(ratios, baseline,
                                             history=history)
                lines += bench.drift_trend_lines(ratios, baseline, history)
                record_advice += sum(1 for ln in lines if "--record" in ln)
                alerting.append(bool(lines))
            after = alerting[len(warm) + len(tail):]
            first_up += 1 if after[0] else 0
            if False in after:
                k = after.index(False) + 1
                clears.append(k)
                stayed += 1 if all(not a for a in after[k:]) else 0
            view = bench.format_history(history, baseline)
            view_none += 1 if (
                "nothing below the floor and no repeat drift" in view) else 0
        n = args.routing_trials
        med = f"{statistics.median(clears):.0f}" if clears else "-"
        slowest = f"{max(clears)}" if clears else "-"
        cleared = len(clears) / n
        print(f"{jit:>7.2f}{cleared:>9.0%}{med:>10}{slowest:>9}"
              f"{first_up / n:>11.0%}{stayed / n:>14.0%}"
              f"{view_none / n:>11.0%}{record_advice:>10}")
        rows.append({
            "kind": "recovery", "model": "recovery",
            "jitter": f"{jit:.2f}", "trials": n,
            "cleared_rate": f"{cleared:.4f}",
            "clear_med": med, "clear_max": slowest,
            "first_run_up_rate": f"{first_up / n:.4f}",
            "stayed_clear_rate": f"{stayed / n:.4f}",
            "view_none_rate": f"{view_none / n:.4f}",
            "record_advice": record_advice,
        })
    return rows


# ---- S6: STALL then RELAPSE -------------------------------------------
# A fix can be partial: the phase gets better (above the floor) but not
# fixed, and later regresses again. Breadth/persistence routing must
# FOLLOW the evidence both ways — the alert ages out while the window is
# clear, stays quiet while it is, then comes back through rerun and
# reaches fix on the fitted bar for the SECOND regression. A route that
# sticks at "fix" forever would be as wrong as one that never fires.
RELAPSE_DEPTH = 0.10          # second regression depth
RELAPSE_STALL_DEPTH = 0.35    # partial recovery: slow, but above the floor
RELAPSE_STALL_RUNS = 10       # long enough to empty the 10-run window
RELAPSE_RUNS = 12             # second regression: long enough to re-fix
RELAPSE_JITTERS = [0.0, 0.15]


def _relapse_timeline(baseline, observed, floor, jitter, rng):
    """(warm, regressed, stalled, relapsed) runs for a stall/relapse story.

    The stall holds the phase at RELAPSE_STALL_DEPTH x baseline — clear
    of the drift floor, so only the window memory keeps any alert up —
    for a full window, then the second regression lands.
    """
    warm, tail = _routing_timeline(
        baseline, observed, floor, jitter, rng, RECOVERY_REGRESSION,
        ("none", 0.0, None), False)
    bars = _phase_bars(baseline)

    def shaped(depth, runs):
        out = []
        for _ in range(runs):
            run = {p: bar * (1.0 + rng.uniform(-jitter, jitter))
                   for p, bar in bars.items()}
            run[PHASE] = (bars[PHASE] * depth
                          * (1.0 + rng.uniform(-jitter, jitter)))
            out.append(run)
        return out

    return (warm, tail, shaped(RELAPSE_STALL_DEPTH, RELAPSE_STALL_RUNS),
            shaped(RELAPSE_DEPTH, RELAPSE_RUNS))


def relapse_probe(baseline, observed, floor, jitter, args):
    """S6: the route must age out in the stall and re-escalate on relapse."""
    print("\n=== S6 stall then relapse (a partial fix clears the alert, a "
          "second regression must re-arm it: rerun -> fix on the bar) ===")
    print(f"{'jitter':>7}{'cleared':>9}{'stall clear':>13}{'quiet':>7}"
          f"{'rerun':>7}{'re-fix':>8}{'ends fix':>10}{'view fix':>10}"
          f"{'--record':>10}")
    rows = []
    for jit in RELAPSE_JITTERS:
        clears, reruns, refixes = [], [], []
        quiet = end_fix = view_fix = 0
        record_advice = 0
        for t in range(args.routing_trials):
            rng = random.Random(f"relapse|{jit:g}|{t}")
            warm, tail, stalled, relapsed = _relapse_timeline(
                baseline, observed, floor, jit, rng)
            history, alerting, routes = [], [], []
            for run in warm + tail + stalled + relapsed:
                ratios = {p: round(v, 2) for p, v in run.items()}
                history.append({"phases": ratios, "host": HOST})
                lines = bench.drift_warnings(ratios, baseline,
                                             history=history)
                lines += bench.drift_trend_lines(ratios, baseline, history)
                record_advice += sum(1 for ln in lines if "--record" in ln)
                alerting.append(bool(lines))
                routes.append(_route_of(lines))
            start_stall = len(warm) + len(tail)
            stall = alerting[start_stall:start_stall + len(stalled)]
            if False in stall:
                k = stall.index(False) + 1
                clears.append(k)
                quiet += 1 if all(not a for a in stall[k:]) else 0
            rel = routes[start_stall + len(stalled):]
            rerun_at = next((i + 1 for i, r in enumerate(rel)
                             if r == "rerun"), None)
            fix_at = next((i + 1 for i, r in enumerate(rel)
                           if r == "fix"), None)
            if rerun_at is not None:
                reruns.append(rerun_at)
            if fix_at is not None:
                refixes.append(fix_at)
            end_fix += 1 if rel and rel[-1] == "fix" else 0
            view = bench.format_history(history, baseline)
            view_fix += 1 if "fix the regressed read path" in view else 0
        n = args.routing_trials
        clear_med = f"{statistics.median(clears):.0f}" if clears else "-"
        rerun_med = f"{statistics.median(reruns):.0f}" if reruns else "-"
        fix_med = f"{statistics.median(refixes):.0f}" if refixes else "-"
        print(f"{jit:>7.2f}{len(clears) / n:>9.0%}{clear_med:>13}"
              f"{quiet / n:>7.0%}{rerun_med:>7}{fix_med:>8}"
              f"{end_fix / n:>10.0%}{view_fix / n:>10.0%}"
              f"{record_advice:>10}")
        rows.append({
            "kind": "relapse", "model": "relapse",
            "jitter": f"{jit:.2f}", "trials": n,
            "cleared_rate": f"{len(clears) / n:.4f}",
            "stall_clear_med": clear_med,
            "quiet_rate": f"{quiet / n:.4f}",
            "rerun_after_med": rerun_med,
            "refix_med": fix_med,
            "end_fix_rate": f"{end_fix / n:.4f}",
            "view_fix_rate": f"{view_fix / n:.4f}",
            "record_advice": record_advice,
        })
    return rows


# ---- S7: STUCK-alert escalation onset and clearing ---------------------
# The stuck alert is gated on a CONSECUTIVE-LOW streak longer than the
# window (STUCK_MIN_STREAK). A deep regression always reaches it; a
# MARGINAL one near the floor flickers above it, resets the streak and
# delays (or prevents) escalation. Measuring both is the point: the
# wording must never escalate before the streak is real, and once it has
# escalated it must still age out with healthy runs.
STUCK_RUNS = 24
STUCK_SHAPES = [("deep", 0.12), ("marginal", 0.22)]
STUCK_JITTERS = [0.0, 0.10, 0.15, 0.25]
STUCK_RECOVERY_RUNS = 10


def _stuck_timeline(baseline, observed, jitter, rng, depth):
    """(warm, regressed, recovered) runs with jitter ON the regression too.

    The regressed value carries the same +/-jitter as a healthy draw, so
    a marginal regression can legitimately step back above the floor.
    """
    bars = _phase_bars(baseline)
    warm = [_healthy_run(bars, observed, jitter, rng)
            for _ in range(ROUTING_WARMUP)]
    regressed = []
    for _ in range(STUCK_RUNS):
        run = {p: bar * (1.0 + rng.uniform(-jitter, jitter))
               for p, bar in bars.items()}
        run[PHASE] = (bars[PHASE] * depth
                      * (1.0 + rng.uniform(-jitter, jitter)))
        regressed.append(run)
    recovered = [_healthy_run(bars, observed, jitter, rng)
                 for _ in range(STUCK_RECOVERY_RUNS)]
    return warm, regressed, recovered


def stuck_probe(baseline, observed, floor, jitter, args):
    """S7: escalation only on a real streak, and it still ages out."""
    print("\n=== S7 stuck-alert escalation (escalates only after a real "
          f"{bench.STUCK_MIN_STREAK}+ consecutive-low streak; a deep or "
          "marginal regression; clears after healthy runs) ===")
    print(f"{'shape':<10}{'jitter':>7}{'escalated':>11}{'onset':>7}"
          f"{'early':>7}{'up at end':>11}{'clear at':>10}"
          f"{'slowest':>9}{'--record':>10}")
    rows = []
    for shape, depth in STUCK_SHAPES:
        for jit in STUCK_JITTERS:
            onsets, clears, alert_end = [], [], 0
            escalated = early = 0
            record_advice = 0
            for t in range(args.routing_trials):
                rng = random.Random(f"stuck|{shape}|{jit:g}|{t}")
                warm, regressed, recovered = _stuck_timeline(
                    baseline, observed, jit, rng, depth)
                history, onset, clear_at = [], None, None
                end_idx = len(warm) + len(regressed)
                for i, run in enumerate(warm + regressed + recovered):
                    ratios = {p: round(v, 2) for p, v in run.items()}
                    history.append({"phases": ratios, "host": HOST})
                    lines = bench.drift_warnings(ratios, baseline,
                                                 history=history)
                    lines += bench.drift_trend_lines(ratios, baseline,
                                                     history)
                    record_advice += sum(1 for ln in lines
                                         if "--record" in ln)
                    stuck_now = any("not holding" in ln for ln in lines)
                    if stuck_now and onset is None:
                        onset = i - len(warm) + 1
                        if (bench.low_streak(PHASE, history, floor)
                                < bench.STUCK_MIN_STREAK):
                            early += 1
                    if i == end_idx - 1:
                        alert_end += 1 if lines else 0
                    if i >= end_idx and clear_at is None and not lines:
                        clear_at = i - end_idx + 1
                if onset is not None:
                    escalated += 1
                    onsets.append(onset)
                if clear_at is not None:
                    clears.append(clear_at)
            n = args.routing_trials
            onset_med = (f"{statistics.median(onsets):.0f}" if onsets
                         else "-")
            clear_med = (f"{statistics.median(clears):.0f}" if clears
                         else "-")
            clear_max = f"{max(clears)}" if clears else "-"
            print(f"{shape:<10}{jit:>7.2f}{escalated / n:>11.0%}"
                  f"{onset_med:>7}{early:>7}{alert_end / n:>11.0%}"
                  f"{clear_med:>10}{clear_max:>9}{record_advice:>10}")
            rows.append({
                "kind": "stuck", "model": "stuck", "shape": shape,
                "jitter": f"{jit:.2f}", "trials": n,
                "escalated_rate": f"{escalated / n:.4f}",
                "onset_med": onset_med, "early": early,
                "alert_end_rate": f"{alert_end / n:.4f}",
                "clear_med": clear_med, "clear_max": clear_max,
                "record_advice": record_advice,
            })
    return rows


# ---- S8: the stuck alert across TWO hosts ------------------------------
# B runs every 2nd step and regresses on EVERY one of its own runs while
# A stays healthy in between. Truth: B is the regressed host, so a stuck
# alert must NAME B (a line naming A is misattribution), may only
# escalate once B's OWN consecutive-low streak passes the window, and
# must clear when B's healthy runs return. `pooled streak` is the same
# count without host scoping: the healthy host resets it every other
# run, which is exactly why the escalation counts per host.
STUCK_TWO_HOST_PAIRS = 6       # B runs before the regression starts
STUCK_TWO_HOST_B_RUNS = STUCK_RUNS   # B's OWN regressed runs
STUCK_B_KEY = (HOST_B["platform"], HOST_B["python"])


def _two_host_stuck_timeline(baseline, observed, jitter, rng, depth):
    """(seq, reg_start, reg_end) of (host label, run) with B regressed.

    A and B alternate 1:1 throughout, so B's own window spans twice the
    shared window — the case a pooled streak can never escalate.
    """
    bars = _phase_bars(baseline)
    seq = []
    for _ in range(STUCK_TWO_HOST_PAIRS):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
    start = len(seq)
    for _ in range(STUCK_TWO_HOST_B_RUNS):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        run = _healthy_run(bars, observed, jitter, rng)
        run[PHASE] = (bars[PHASE] * depth
                      * (1.0 + rng.uniform(-jitter, jitter)))
        seq.append(("B", run))
    end = len(seq)
    for _ in range(STUCK_RECOVERY_RUNS):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
    return seq, start, end


def stuck_two_host_probe(baseline, observed, floor, jitter, args):
    """S8: the stuck alert names the regressed host, then clears."""
    print("\n=== S8 stuck alert across two hosts (B regresses on every one "
          "of its own runs, A healthy in between; truth: B is the "
          "regressed host, so the stuck line must name B and never A) ===")
    print(f"{'shape':<10}{'jitter':>7}{'escalated':>11}{'named B':>9}"
          f"{'named A':>9}{'onset':>7}{'early':>7}{'pooled':>8}"
          f"{'up at end':>11}{'clear at':>10}{'slowest':>9}{'--record':>10}")
    rows = []
    for shape, depth in STUCK_SHAPES:
        for jit in STUCK_JITTERS:
            escalated = named_b = named_a = alert_end = 0
            early = record_advice = 0
            onsets, clears, pooled = [], [], []
            for t in range(args.routing_trials):
                rng = random.Random(f"stuck2|{shape}|{jit:g}|{t}")
                seq, start, end = _two_host_stuck_timeline(
                    baseline, observed, jit, rng, depth)
                history = []
                onset, clear_at = None, None
                b_seen = b_recovered = 0
                for i, (lab, run) in enumerate(seq):
                    ratios = {p: round(v, 2) for p, v in run.items()}
                    history.append({"phases": ratios,
                                    "host": HOST_B if lab == "B" else HOST})
                    if lab == "B":
                        if start <= i < end:
                            b_seen += 1
                        elif i >= end:
                            b_recovered += 1
                    lines = bench.drift_warnings(ratios, baseline,
                                                 history=history)
                    lines += bench.drift_trend_lines(ratios, baseline,
                                                     history)
                    record_advice += sum(1 for ln in lines
                                         if "--record" in ln)
                    stuck_lines = [ln for ln in lines
                                   if "not holding" in ln]
                    if stuck_lines and onset is None:
                        onset = b_seen
                        if (bench.low_streak(PHASE, history, floor,
                                             STUCK_B_KEY)
                                < bench.STUCK_MIN_STREAK):
                            early += 1
                        if any(HOST_B["platform"] in ln
                               for ln in stuck_lines):
                            named_b += 1
                        elif any(HOST["platform"] in ln
                                 for ln in stuck_lines):
                            named_a += 1
                    if i == end - 1:
                        alert_end += 1 if lines else 0
                        # Without host scoping the interleaved healthy
                        # host breaks the count every other run.
                        pooled.append(bench.low_streak(PHASE, history,
                                                       floor))
                    if i >= end and lab == "B" and clear_at is None \
                            and not lines:
                        clear_at = b_recovered
                if onset is not None:
                    escalated += 1
                    onsets.append(onset)
                if clear_at is not None:
                    clears.append(clear_at)
            n = args.routing_trials
            onset_med = (f"{statistics.median(onsets):.0f}" if onsets
                         else "-")
            clear_med = (f"{statistics.median(clears):.0f}" if clears
                         else "-")
            clear_max = f"{max(clears)}" if clears else "-"
            pooled_med = (f"{statistics.median(pooled):.0f}" if pooled
                          else "-")
            print(f"{shape:<10}{jit:>7.2f}{escalated / n:>11.0%}"
                  f"{named_b / n:>9.0%}{named_a / n:>9.0%}"
                  f"{onset_med:>7}{early:>7}"
                  f"{pooled_med:>8}{alert_end / n:>11.0%}"
                  f"{clear_med:>10}{clear_max:>9}{record_advice:>10}")
            rows.append({
                "kind": "stuck_twohost", "model": "twohost",
                "shape": shape, "jitter": f"{jit:.2f}", "trials": n,
                "escalated_rate": f"{escalated / n:.4f}",
                "onset_med": onset_med, "early": early,
                "named_b_share": f"{named_b / n:.4f}",
                "named_a_share": f"{named_a / n:.4f}",
                "pooled_streak": pooled_med,
                "alert_end_rate": f"{alert_end / n:.4f}",
                "clear_med": clear_med, "clear_max": clear_max,
                "record_advice": record_advice,
            })
    return rows


# ---- S9: host-wide slide on an interleaved machine ---------------------
# B slides EVERY phase on its own runs while A stays healthy, 1:1. The
# host-wide route must be "rule the host out", counted in B's own runs —
# the same slide read pooled is 5 of 10, below the 6-of-10 bar. The
# negative control is the SAME slide from a brand-new host: two own runs
# must stay transient ("rerun") rather than claim a machine-wide slide.
SHARED_SLIDE_DEPTHS = [0.12, 0.20]
SHARED_SLIDE_RECOVERY = 10

def _shared_slide_timeline(baseline, observed, jitter, rng, depth, b_runs,
                           warm_pairs=0, prefix_healthy=0):
    """(seq, reg_start, reg_end) of (host label, run) for an all-phase
    slide on B's own runs, A healthy in between."""
    bars = _phase_bars(baseline)
    seq = [("A", _healthy_run(bars, observed, jitter, rng))
           for _ in range(prefix_healthy)]
    for _ in range(warm_pairs):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
    start = len(seq)
    for _ in range(b_runs):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        seq.append(("B", {p: bar * depth * (1.0 + rng.uniform(-jitter, jitter))
                          for p, bar in bars.items()}))
    end = len(seq)
    for _ in range(SHARED_SLIDE_RECOVERY):
        seq.append(("A", _healthy_run(bars, observed, jitter, rng)))
        seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
    return seq, start, end


def shared_slide_two_host_probe(baseline, observed, floor, jitter, args):
    """S9: a host-wide slide on one of two hosts, and its thin control."""
    print("\n=== S9 host-wide slide across two hosts (B slides EVERY phase "
          "on its own runs, A healthy in between; truth: the MACHINE is "
          "slow, so the route must be \"rule the host out\", never one "
          "read path) ===")
    print(f"{'shape':<10}{'depth':>7}{'route host':>12}{'route fix':>11}"
          f"{'sustained':>11}{'pooled sust':>13}{'named B':>9}"
          f"{'named A':>9}{'own ev':>8}{'pooled ev':>11}"
          f"{'clear at':>10}{'--record':>10}")
    shapes = (("sustained", 12, 6, 0), ("thin", 2, 0, 10))
    rows = []
    for depth in SHARED_SLIDE_DEPTHS:
        for shape, b_runs, warm, prefix in shapes:
            host_routes = fix_routes = 0
            sustained_claims = pooled_sustained = 0
            named_b = named_a = record_advice = 0
            own_ev, pool_ev, clears = [], [], []
            for t in range(args.routing_trials):
                rng = random.Random(f"sharedslide|{depth:g}|{shape}|{t}")
                seq, start, end = _shared_slide_timeline(
                    baseline, observed, jitter, rng, depth, b_runs, warm,
                    prefix)
                history, clear_at, b_recovered = [], None, 0
                for i, (lab, run) in enumerate(seq):
                    ratios = {p: round(v, 2) for p, v in run.items()}
                    history.append({"phases": ratios,
                                    "host": HOST_B if lab == "B" else HOST})
                    if i >= end and lab == "B":
                        b_recovered += 1
                    lines = bench.drift_warnings(ratios, baseline,
                                                 history=history)
                    lines += bench.drift_trend_lines(ratios, baseline,
                                                     history)
                    record_advice += sum(1 for ln in lines
                                         if "--record" in ln)
                    if i == end - 1:
                        route = bench.route_of(lines)
                        host_routes += 1 if route == "host" else 0
                        fix_routes += 1 if route == "fix" else 0
                        # The sustained wording is the claim under test
                        # ("have slid together"): both forms route to
                        # "host", only this one asserts persistence.
                        sustained_claims += (
                            1 if any("have slid together" in ln
                                     for ln in lines) else 0)
                        # The old pooled rule, measured on the same
                        # history with host labels stripped.
                        pooled_hist = [dict(e, host=None) for e in history]
                        pooled_lines = bench.drift_warnings(
                            ratios, baseline, history=pooled_hist)
                        pooled_lines += bench.drift_trend_lines(
                            ratios, baseline, pooled_hist)
                        pooled_sustained += (
                            1 if any("have slid together" in ln
                                     for ln in pooled_lines) else 0)
                        if any(HOST_B["platform"] in ln for ln in lines):
                            named_b += 1
                        elif any(HOST["platform"] in ln for ln in lines):
                            named_a += 1
                        own = bench.phase_evidence(
                            PHASE, history, floor, host_key=STUCK_B_KEY)
                        pool = bench.phase_evidence(PHASE, history, floor)
                        own_ev.append(f"{own[0]}/{own[1]}")
                        pool_ev.append(f"{pool[0]}/{pool[1]}")
                    if (i >= end and lab == "B" and clear_at is None
                            and not lines):
                        clear_at = b_recovered
                if clear_at is not None and shape == "sustained":
                    clears.append(clear_at)
            n = args.routing_trials
            own_mode = max(set(own_ev), key=own_ev.count) if own_ev else "-"
            pool_mode = max(set(pool_ev), key=pool_ev.count) if pool_ev \
                else "-"
            clear_med = (f"{statistics.median(clears):.0f}" if clears
                         else "-")
            print(f"{shape:<10}{depth:>7.2f}{host_routes / n:>12.0%}"
                  f"{fix_routes / n:>11.0%}{sustained_claims / n:>11.0%}"
                  f"{pooled_sustained / n:>13.0%}"
                  f"{named_b / n:>9.0%}{named_a / n:>9.0%}"
                  f"{own_mode:>8}{pool_mode:>11}{clear_med:>10}"
                  f"{record_advice:>10}")
            rows.append({
                "kind": "shared_twohost", "model": "twohost",
                "shape": shape, "jitter": f"{jitter:.2f}",
                "level": f"{depth:.2f}", "trials": n,
                "host_share": f"{host_routes / n:.4f}",
                "fix_share": f"{fix_routes / n:.4f}",
                "sustained_share": f"{sustained_claims / n:.4f}",
                "pooled_sustained_share": f"{pooled_sustained / n:.4f}",
                "named_b_share": f"{named_b / n:.4f}",
                "named_a_share": f"{named_a / n:.4f}",
                "own_evidence": own_mode,
                "pooled_evidence": pool_mode,
                "clear_med": clear_med, "record_advice": record_advice,
            })
    return rows


# ---- S10: a host SWITCH mid-history ------------------------------------
# A runs first and regresses for 24 of its own runs (long past stuck),
# then the runs move to B. Two truths: B healthy -> the alert must be
# GONE on B's first run (the memory is the machine's own), and B slow ->
# the stuck alert must be EARNED again over B's own runs (11), never
# inherited from A's streak. A's still-open alert may be REPORTED after
# the switch (marked context), but it may never become B's verdict —
# the old code re-posted A's "unresolved REGRESSION" on every clean B
# run and routed it to "fix the regressed read path" on a machine that
# was fine.
HOST_SWITCH_WARM = 6
HOST_SWITCH_A_LOW = 24
HOST_SWITCH_B_RUNS = 12
HOST_SWITCH_RECOVERY = 6


def _host_switch_timeline(baseline, observed, jitter, rng, b_depth):
    """(seq, switch_idx, reg_end) with A regressed then B taking over."""
    bars = _phase_bars(baseline)
    seq = [("A", _healthy_run(bars, observed, jitter, rng))
           for _ in range(HOST_SWITCH_WARM)]
    for _ in range(HOST_SWITCH_A_LOW):
        run = _healthy_run(bars, observed, jitter, rng)
        run[PHASE] = (bars[PHASE] * 0.12
                      * (1.0 + rng.uniform(-jitter, jitter)))
        seq.append(("A", run))
    switch = len(seq)
    for _ in range(HOST_SWITCH_B_RUNS):
        if b_depth is None:
            seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
        else:
            run = _healthy_run(bars, observed, jitter, rng)
            run[PHASE] = (bars[PHASE] * b_depth
                          * (1.0 + rng.uniform(-jitter, jitter)))
            seq.append(("B", run))
    end = len(seq)
    for _ in range(HOST_SWITCH_RECOVERY):
        seq.append(("B", _healthy_run(bars, observed, jitter, rng)))
    return seq, switch, end


def host_switch_probe(baseline, observed, floor, jitter, args):
    """S10: the alert follows the machine that is running now."""
    print("\n=== S10 host switch (A has been stuck for 24 of its own runs, "
          "then the runs move to B; truth: B's record is the only one "
          "that matters after the switch) ===")
    print(f"{'shape':<17}{'clean 1st':>10}{'A leak':>8}{'named B':>9}"
          f"{'ctx A':>7}{'onset':>7}{'early':>7}{'stuck 1st':>11}"
          f"{'clear at':>10}{'--record':>10}")
    rows = []
    for shape, b_depth in (("fixed by switch", None),
                           ("new host slow", 0.12)):
        clean = leak = stuck_1st = early = record_advice = 0
        named_b_runs = 0
        onsets, clears, ctx_counts = [], [], []
        for t in range(args.routing_trials):
            rng = random.Random(f"switch|{shape}|{t}")
            seq, switch, end = _host_switch_timeline(
                baseline, observed, jitter, rng, b_depth)
            history, onset, clear_at = [], None, None
            b_own = b_recovered = ctx_here = 0
            for i, (lab, run) in enumerate(seq):
                ratios = {p: round(v, 2) for p, v in run.items()}
                history.append({"phases": ratios,
                                "host": HOST_B if lab == "B" else HOST})
                if lab == "B":
                    if i < end:
                        b_own += 1
                    else:
                        b_recovered += 1
                lines = bench.drift_warnings(ratios, baseline,
                                             history=history)
                lines += bench.drift_trend_lines(ratios, baseline, history)
                record_advice += sum(1 for ln in lines
                                     if "--record" in ln)
                stuck_lines = [ln for ln in lines
                               if "not holding" in ln]
                # Only B's OWN stuck alert counts after the switch;
                # A's unresolved one is a different machine's news.
                b_stuck = [ln for ln in stuck_lines
                           if HOST_B["platform"] in ln]
                verdicts = [ln for ln in lines
                            if not ln.startswith(bench.CONTEXT_MARK)]
                context = [ln for ln in lines
                           if ln.startswith(bench.CONTEXT_MARK)]
                if i == switch:          # B's first run after the switch
                    clean += 1 if bench.route_of(lines) == "none" else 0
                    stuck_1st += 1 if b_stuck else 0
                    # A's alert may be REPORTED, never re-posted as a
                    # verdict on a run A did not take.
                    leak += 1 if any(HOST["platform"] in ln
                                     for ln in verdicts) else 0
                if i >= switch:
                    named_b_runs += 1 if any(HOST_B["platform"] in ln
                                             for ln in verdicts) else 0
                    if any(HOST["platform"] in ln for ln in context):
                        ctx_here += 1
                if b_stuck and onset is None and i >= switch:
                    onset = b_own
                    if (bench.low_streak(PHASE, history, floor, STUCK_B_KEY)
                            < bench.STUCK_MIN_STREAK):
                        early += 1
                # The alert is gone when nothing routes: a context line
                # about another machine is not this run's alert.
                if i >= end and clear_at is None \
                        and bench.route_of(lines) == "none":
                    clear_at = b_recovered
            ctx_counts.append(ctx_here)
            if onset is not None:
                onsets.append(onset)
                # "Cleared" only means something for a run that had an
                # alert: a switch to a healthy machine was already quiet
                # on arrival (that is `clean 1st`), not a recovery.
                if clear_at is not None:
                    clears.append(clear_at)
        n = args.routing_trials
        post = n * (HOST_SWITCH_B_RUNS + HOST_SWITCH_RECOVERY)
        onset_med = (f"{statistics.median(onsets):.0f}" if onsets else "-")
        clear_med = (f"{statistics.median(clears):.0f}" if clears else "-")
        ctx_med = f"{statistics.median(ctx_counts):.0f}" if ctx_counts else "-"
        print(f"{shape:<17}{clean / n:>10.0%}{leak / n:>8.0%}"
              f"{named_b_runs / post:>9.0%}{ctx_med:>7}{onset_med:>7}"
              f"{early:>7}{stuck_1st / n:>11.0%}{clear_med:>10}"
              f"{record_advice:>10}")
        rows.append({
            "kind": "hostswitch", "model": "switch", "shape": shape,
            "jitter": f"{jitter:.2f}", "trials": n,
            "clean_share": f"{clean / n:.4f}",
            "leak_share": f"{leak / n:.4f}",
            "stuck_share": f"{stuck_1st / n:.4f}",
            "onset_med": onset_med,
            "named_b_share": f"{named_b_runs / post:.4f}",
            "context_med": ctx_med,
            "early": early, "clear_med": clear_med,
            "record_advice": record_advice,
        })
    return rows


# ---- S10b: the idle host comes BACK ------------------------------------
# A is still stuck when the runs move to B for 12 runs, then A runs
# again. Its own record is intact, so the first run back must be a
# saturated stuck alert naming A — the shared-window rule had only 1 of
# A's runs left in the window and had to wait for 2 more just to reach
# the minimum slice.


def _a_low_run(bars, observed, jitter, rng, depth=0.12):
    run = _healthy_run(bars, observed, jitter, rng)
    run[PHASE] = bars[PHASE] * depth * (1.0 + rng.uniform(-jitter, jitter))
    return run


def host_return_probe(baseline, observed, floor, jitter, args):
    """S10b: an idle host comes back to its OWN record, not an empty one."""
    print("\n=== S10b idle host returns (A is still stuck; after 12 runs on "
          "B it runs again; truth: A's own window is intact, so its "
          "saturated alert is back on that first run) ===")
    print(f"{'shape':<14}{'alert back':>12}{'named A':>9}{'named B':>9}"
          f"{'own ev':>8}{'old slice':>11}{'--record':>10}")
    alert = named_a = named_b = record_advice = 0
    ev, old = [], []
    bars = _phase_bars(baseline)
    for t in range(args.routing_trials):
        rng = random.Random(f"return|{t}")
        seq, _switch, _end = _host_switch_timeline(
            baseline, observed, jitter, rng, None)
        seq.append(("A", _a_low_run(bars, observed, jitter, rng)))
        history = []
        for lab, run in seq:
            history.append({"phases": {p: round(v, 2) for p, v in run.items()},
                            "host": HOST_B if lab == "B" else HOST})
        ratios = dict(history[-1]["phases"])
        lines = bench.drift_warnings(ratios, baseline, history=history)
        lines += bench.drift_trend_lines(ratios, baseline, history)
        record_advice += sum(1 for ln in lines if "--record" in ln)
        stuck = [ln for ln in lines if "not holding" in ln]
        alert += 1 if any(HOST["platform"] in ln for ln in stuck) else 0
        named_a += 1 if any(HOST["platform"] in ln for ln in lines) else 0
        named_b += 1 if any(HOST_B["platform"] in ln for ln in lines) else 0
        low, seen = bench.phase_evidence(PHASE, history, floor,
                                         host_key=(HOST["platform"],
                                                   HOST["python"]))
        ev.append(f"{low}/{seen}")
        # the OLD rule: A's share of the last 10 entries overall
        old.append(sum(1 for e in history[-bench.TREND_WINDOW:]
                       if e["host"] == HOST))
    n = args.routing_trials
    own_mode = max(set(ev), key=ev.count) if ev else "-"
    old_med = f"{statistics.median(old):.0f}" if old else "-"
    print(f"{'idle host':<14}{alert / n:>12.0%}{named_a / n:>9.0%}"
          f"{named_b / n:>9.0%}{own_mode:>8}{old_med:>11}"
          f"{record_advice:>10}")
    return [{
        "kind": "hostreturn", "model": "return", "shape": "idle host",
        "jitter": f"{jitter:.2f}", "trials": n,
        "rate": f"{alert / n:.4f}",
        "named_a_share": f"{named_a / n:.4f}",
        "named_b_share": f"{named_b / n:.4f}",
        "own_evidence": own_mode, "thin": old_med,
        "record_advice": record_advice,
    }]


CSV_FIELDS = ["kind", "model", "level", "label", "window", "threshold",
              "rho", "fraction", "band", "regime", "slide", "noise",
              "regression", "burst", "fix_rate", "host_rate", "rerun_rate",
              "none_rate", "trend_rate", "record_advice", "trials", "fired",
              "rate", "named_a", "named_b", "unlabeled",
              "lat_total_med", "lat_host_med", "missed",
              "worst_ff", "mean_ff", "fit_ok", "fit_best", "current",
              "thin", "settled", "mean_step",
              "lat_min", "lat_median", "lat_max", "lat_missed",
              "jitter", "reach_rate", "rerun_med", "fix_med", "escal_med",
              "depth_med", "bar_rate", "premature",
              "cleared_rate", "clear_med", "clear_max",
              "first_run_up_rate", "stayed_clear_rate", "view_none_rate",
              "stall_clear_med", "quiet_rate", "rerun_after_med",
              "refix_med", "end_fix_rate", "view_fix_rate",
              "shape", "escalated_rate", "onset_med", "early",
              "alert_end_rate", "clear_max", "pooled_streak",
              "named_b_share", "named_a_share",
              "host_share", "fix_share", "sustained_share",
              "pooled_sustained_share",              "own_evidence", "pooled_evidence", "clean_share",
              "stuck_share", "leak_share", "context_med"]


def write_csv(path, rows):
    """All measurement rows (false-fire + latency) as one flat CSV."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({f: r.get(f, "") for f in CSV_FIELDS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=100,
                    help="simulated histories per setting/model/level")
    ap.add_argument("--steps", type=int, default=150,
                    help="noisy runs per false-fire trial (from empty)")
    ap.add_argument("--warmup", type=int, default=30,
                    help="healthy runs prepended by the LATENCY probe "
                         "only (so the slice is full before regressing)")
    ap.add_argument("--jitter", type=float, default=0.15,
                    help="healthy-run multiplicative noise bound")
    ap.add_argument("--latency-trials", type=int, default=30)
    ap.add_argument("--latency-cap", type=int, default=40,
                    help="regressed runs to allow before 'missed'")
    ap.add_argument("--iid-levels", type=float, nargs="+",
                    default=[0.03, 0.06, 0.10, 0.20],
                    help="per-run independent dip probabilities")
    ap.add_argument("--ar1-levels", type=float, nargs="+",
                    default=[0.05, 0.10, 0.20],
                    help="AR(1) marginal dip probabilities (the clustered "
                         "model of record)")
    ap.add_argument("--ar1-rhos", type=float, nargs="+", default=[0.5, 0.8],
                    help="AR(1) persistence values (mean cluster length "
                         "grows with rho)")
    ap.add_argument("--bursty-levels", type=float, nargs="+", default=[],
                    help="RETIRED burst+idle model, off by default: "
                         "burst-START probabilities (1-4 run spikes)")
    ap.add_argument("--routing-trials", type=int, default=20,
                    help="simulated timelines per routing-matrix cell")
    ap.add_argument("--refit", action="store_true",
                    help="also grid-search the threshold policy under "
                         "AR(1) (window x scaling fraction) and report "
                         "the fitted best vs production")
    ap.add_argument("--fit-levels", type=float, nargs="+",
                    default=[0.05, 0.10],
                    help="AR(1) marginal rates the re-fit optimizes over "
                         "(ordinary noise; heavier bands saturate and "
                         "belong to the re-record signal)")
    ap.add_argument("--fit-trials", type=int, default=80,
                    help="trials per AR(1) band in the policy re-fit "
                         "(80 is what the CONTRIBUTING grid was fit on)")
    ap.add_argument("--fit-steps", type=int, default=120,
                    help="steps per re-fit false-fire trial")
    ap.add_argument("--fit-max-latency", type=float, default=6.0,
                    help="median regressed runs a fitted policy may take "
                         "to catch, i.e. the guard against fitting a "
                         "policy that never fires")
    ap.add_argument("--csv", metavar="PATH", default=None,
                    help="also write every measured row (false-fire + "
                         "latency) to this CSV for scripts/"
                         "render_stress_matrix.py")
    args = ap.parse_args(argv)

    baseline, observed = load_corpus()
    floor = baseline["phases"][PHASE] * FLOOR
    # Healthy draws must never reach the drift floor, or ALL settings
    # would see dips that exist in no real host. Clamp the jitter so
    # min(observed) x (1-jitter) stays strictly above the floor.
    jitter = min(args.jitter,
                 max(0.0, 1.0 - floor / min(observed)) - 1e-6)
    if jitter <= 0:
        sys.exit("observed corpus already at the drift floor; a safe "
                 "jitter cannot be constructed")

    print(f"baseline growth = {baseline['phases'][PHASE]:.2f}x, "
          f"drift floor = {floor:.1f}x")
    print(f"observed healthy ratios: n={len(observed)}, "
          f"min={min(observed):.1f}x, max={max(observed):.1f}x "
          f"(margin to floor >= {min(observed) / floor:.2f}x)")
    print(f"healthy-jitter = +/-{jitter:.2f} (clamped: a healthy draw can "
          "never reach the floor)")
    print(f"trials={args.trials}/setting, steps={args.steps} "
          f"(false-fire trials start empty; latency probe warms "
          f"{args.warmup})")

    csv_rows = []
    for model, levels, rhos in (("iid", args.iid_levels, [None]),
                               ("ar1", args.ar1_levels, args.ar1_rhos),
                               ("bursty", args.bursty_levels, [None])):
        for rho in rhos:
            for level in levels:
                rows = false_fire_rows(baseline, observed, floor, jitter,
                                       args, model, level, rho)
                print_section(model, level, rows, args.trials, rho)
                for r in rows:
                    csv_rows.append({
                        "kind": "falsefire", "model": model,
                        "rho": "" if rho is None else f"{rho:g}",
                        "level": f"{level:g}", "label": r["label"],
                        "window": r["w"], "threshold": r["thr"],
                        "trials": args.trials, "fired": r["fired"],
                        "rate": f"{r['rate']:.4f}", "thin": r["thin"],
                        "settled": r["settled"],
                        "mean_step": (f"{r['mean']:.1f}"
                                      if r["mean"] is not None else ""),
                    })

    print(f"{args.trials}/setting trials, warmup {args.warmup} healthy "
          f"interleaved steps before the acted/regressed phase")
    csv_rows += run_acting_b(baseline, observed, floor, jitter, args)
    csv_rows += run_mask_probe(baseline, observed, floor, jitter, args)

    lat_rows = latency_table(baseline, observed, floor, jitter, args)
    for r in lat_rows:
        csv_rows.append({
            "kind": "latency", "model": "none", "level": "",
            "label": r["label"], "window": r["w"], "threshold": r["thr"],
            "trials": args.latency_trials, "fired": r["fired"],
            "lat_min": r["min"], "lat_median": r["median"],
            "lat_max": r["max"], "lat_missed": r["missed"],
        })

    csv_rows += routing_probe(baseline, observed, floor, jitter, args)
    csv_rows += deepening_probe(baseline, observed, floor, jitter, args)
    csv_rows += recovery_probe(baseline, observed, floor, jitter, args)
    csv_rows += relapse_probe(baseline, observed, floor, jitter, args)
    csv_rows += stuck_probe(baseline, observed, floor, jitter, args)
    csv_rows += stuck_two_host_probe(baseline, observed, floor, jitter, args)
    csv_rows += shared_slide_two_host_probe(baseline, observed, floor, jitter,
                                           args)
    csv_rows += host_switch_probe(baseline, observed, floor, jitter, args)
    csv_rows += host_return_probe(baseline, observed, floor, jitter, args)

    if args.refit:
        csv_rows += refit_policy(baseline, observed, floor, jitter, args)

    if args.csv:
        write_csv(args.csv, csv_rows)
        print(f"\nCSV written: {args.csv} ({len(csv_rows)} rows)")


if __name__ == "__main__":
    main()
