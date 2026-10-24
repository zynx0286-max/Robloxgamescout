"""Render the CONTRIBUTING stress-matrix block from a trend-stress CSV.

Reads the flat CSV produced by `trend_stress_test.py --csv PATH` and
prints the Markdown block (plus the HTML "generated" marker) that the
CONTRIBUTING perf-history section embeds. The numbers in CONTRIBUTING
are then always script-rendered from a measured run — nobody types
them, so nobody transcribes them wrong:

  py scripts/trend_stress_test.py --csv /tmp/trend.csv   # full sweep
  py scripts/render_stress_matrix.py /tmp/trend.csv      # print block

Paste the printed block over the previous one between the marker
comments. With the harness's fixed seeds, rerunning with default
parameters reproduces the same numbers unless the noise model, the
settings under sweep, or the functions under test changed. If the CSV
contains the two-host probe rows (kinds `twohost_acting` /
`twohost_mask`) their sections are appended too; a CSV written by an
older harness renders fine without them.

`--check` is the review gate: it renders from the CSV and compares the
result with the block committed in CONTRIBUTING.md, exiting 1 (with the
first divergent lines) when the doc is stale — CI runs it after a fresh
sweep, so a hand-edited or forgotten table cannot land:

  py scripts/render_stress_matrix.py --check /tmp/trend.csv
"""

import argparse
import csv
import difflib
import pathlib
import sys

# The doc that carries the generated block, resolved from this file:
# .../Roblox-Game-Scout-Bot/scripts/ -> the checkout root.
DEFAULT_DOC = pathlib.Path(__file__).resolve().parents[2] / "CONTRIBUTING.md"
BLOCK_START = "<!-- stress-matrix:"
BLOCK_END = "<!-- end stress-matrix -->"


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "kind" not in (reader.fieldnames or []):
            sys.exit("not a trend-stress CSV (missing the 'kind' column) "
                     "— was it written by trend_stress_test.py --csv?")
        rows = list(reader)
    ff = [r for r in rows if r["kind"] == "falsefire"]
    lat = [r for r in rows if r["kind"] == "latency"]
    if not ff or not lat:
        sys.exit("CSV is missing falsefire/latency rows — was it written "
                 "by trend_stress_test.py --csv?")
    return ff, lat, rows


def _pct(rows):
    return [r for r in rows if r["kind"] == "twohost_acting"]


def _mask(rows):
    return [r for r in rows if r["kind"] == "twohost_mask"]


def render(ff, lat, extra):
    # settings in first-seen order = CONFIGS order of the run;
    # sections (model, level) in first-seen order likewise.
    sections = []
    for r in ff:
        key = (r["model"], r.get("rho") or "", r["level"],
               f"{float(r['level']):g}")
        if key not in sections:
            sections.append(key)
    settings = []
    for r in ff:
        if r["label"] not in settings:
            settings.append(r["label"])

    def section_label(model, rho, level):
        if model == "ar1":
            # ASCII label: this renders into a doc on a Windows console.
            return f"ar1 rho{float(rho):g} {float(level):g}"
        return f"{model} {float(level):g}"

    def cell(model, rho, level, label):
        for r in ff:
            if (r["model"] == model and (r.get("rho") or "") == rho
                    and r["level"] == level and r["label"] == label):
                return f"{float(r['rate']):.0%}"
        return "n/a"

    out = []
    out.append("<!-- stress-matrix: GENERATED - do not hand-edit. "
               "Regenerate with:")
    out.append("     py scripts/trend_stress_test.py --refit --csv "
               "/tmp/trend.csv")
    out.append("     py scripts/render_stress_matrix.py /tmp/trend.csv "
               "> /tmp/matrix.md")
    out.append("     (fixed seeds: defaults reproduce these numbers "
               "unless the noise model, sweep settings or the trend "
               "functions change) then replace this block. -->")
    out.append("| noise | " + " | ".join(f"`{s.strip()}`"
                                         for s in settings) + " |")
    out.append("|---" + "|---" * len(settings) + "|")
    for model, rho, level, _ in sections:
        out.append("| " + section_label(model, rho, level) + " | "
                   + " | ".join(cell(model, rho, level, s)
                                for s in settings) + " |")

    out.append("")
    out.append("Detection latency for the sustained regression "
               "(regressed runs until the fire; 0 missed everywhere in "
               "this sweep unless the run says otherwise):")
    for r in lat:
        span = (f"{r['lat_min']}/{r['lat_median']}/{r['lat_max']}"
                if r["lat_min"] != "" else "-")
        out.append(f"- `{r['label'].strip()}`: {span} "
                   f"({r['fired']}/{r['trials']} fired, "
                   f"{r['lat_missed']} missed)")

    # ---- the (window x share) policy grid the fit was decided on ----
    fit = [r for r in extra if r["kind"] == "refit"]
    if fit:
        bands = []
        for r in fit:
            if r["band"] not in bands:
                bands.append(r["band"])
        policies = {}
        for r in fit:
            policies.setdefault((r["window"], r["fraction"]), r)
        out.append("")
        out.append("Policy re-fit under the AR(1) model - grid over window "
                   "x scaling share, each policy's false-fire rate per "
                   "ordinary-noise band (heavy bands saturate and are "
                   "excluded; a policy may wait at most "
                   "`--fit-max-latency` regressed runs):")
        out.append("| window | share | thr | "
                   + " | ".join(bands) + " | worst | latency | mark |")
        out.append("|---|---|---" + "|---" * len(bands)
                   + "|---|---|---|")
        for (w, frac), probe in policies.items():
            cells = []
            for band in bands:
                rate = next((r["rate"] for r in fit
                             if r["window"] == w and r["fraction"] == frac
                             and r["band"] == band), "")
                cells.append(f"{float(rate):.0%}" if rate != "" else "-")
            marks = []
            if probe.get("current") == "1":
                marks.append("**current**")
            if probe.get("fit_best") == "1":
                marks.append("**fitted**")
            if probe.get("fit_ok") != "1":
                marks.append("over latency budget")
            lat = probe.get("lat_median") or "-"
            out.append(f"| {w} | {float(frac):.2f} | {probe['threshold']} | "
                       + " | ".join(cells)
                       + f" | {float(probe['worst_ff']):.0%} | {lat} | "
                       + ", ".join(marks) + " |")

    # ---- routing: breadth decides fix-one-path vs host-wide ----
    routing = [r for r in extra if r["kind"] == "routing"]
    if routing:
        out.append("")
        out.append("Operator routing - truth x noise mix x concurrent host "
                   "burst, routed by BREADTH (which phases slid): `one "
                   "phase` = one phase sliding at that depth x baseline "
                   "while the others hold (the code-regression signature; "
                   "regression `none` is the negative control); `all "
                   "phases` = every phase at 0.15x baseline together (a "
                   "host-wide slide). The concurrent burst drops every "
                   "phase for 3 runs. Each cell is where the last message "
                   "set on that timeline sent the operator; `host` means "
                   "it pointed at the machine/host rather than one read "
                   "path, and the `--record` column counts messages that "
                   "recommended re-recording (must stay 0 everywhere):")
        out.append("| truth | noise | regression | burst | trend fires | "
                   "fix | host | rerun | none | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in routing:
            reg = "none" if not r["regression"] else r["regression"]
            # host_rate is absent in CSVs from before breadth routing
            host = r.get("host_rate") or ""
            host_cell = f"{float(host):.0%}" if host else "- "
            cells = [r.get("slide") or "one phase", r["noise"], reg,
                     r["burst"], f"{float(r['trend_rate']):.0%}",
                     f"{float(r['fix_rate']):.0%}", host_cell,
                     f"{float(r['rerun_rate']):.0%}",
                     f"{float(r['none_rate']):.0%}",
                     r["record_advice"]]
            out.append("| " + " | ".join(cells) + " |")

    # ---- S4: route ESCALATION on a regression that deepens ----
    deepening = [r for r in extra if r["kind"] == "deepening"]
    if deepening:
        out.append("")
        out.append("Escalation on a regression that DEEPENS over time - one "
                   "phase ramps from its committed ratio to 2% of it over "
                   "30 runs (geometric: shallow first, unmistakable later) "
                   "while the others stay healthy (a narrow slide), "
                   "replayed step by step through the REAL messages. "
                   "`rerun run` / `fix run` are median tail runs after "
                   "onset; `depth at fix` is the regression depth when the "
                   "first fix posts; `on bar` is the share of trials whose "
                   "FIRST fix lands exactly on the run where the fitted "
                   "evidence bar is met; `premature` counts fixes posted "
                   "before the bar (must be 0) and `--record` must stay 0:")
        out.append("| jitter | fix reached | rerun run | fix run | rerun->fix | "
                   "depth at fix | on bar | premature | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for r in deepening:
            depth = r.get("depth_med") or ""
            depth_cell = f"{float(depth):.0%}" if depth else "-"
            out.append(
                f"| {float(r['jitter']):.2f} "
                f"| {float(r['reach_rate']):.0%} "
                f"| {r['rerun_med']} | {r['fix_med']} "
                f"| {r['escal_med']} | {depth_cell} "
                f"| {float(r['bar_rate']):.0%} "
                f"| {r['premature']} | {r['record_advice']} |")

    # ---- S5: the alert must CLEAR after the fix ----
    recovery = [r for r in extra if r["kind"] == "recovery"]
    if recovery:
        out.append("")
        out.append("Recovery after the regression is fixed - a 24-run "
                   "regression (saturated 10-run window) followed by "
                   "healthy runs, replayed through the REAL messages and "
                   "the /perfhistory verdict. `clear at` is the healthy "
                   "run on which the warning/trend messages stop (median); "
                   "`up on "
                   "1st` is the share still alerting on the first healthy "
                   "run (the window is the memory, so the alert ages out "
                   "instead of vanishing); `stayed clear` = no alert "
                   "returned afterwards; `view none` = /perfhistory routes "
                   "back to nothing; `--record` must stay 0:")
        out.append("| jitter | cleared | clear at | slowest | up on 1st | "
                   "stayed clear | view none | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in recovery:
            out.append(
                f"| {float(r['jitter']):.2f} "
                f"| {float(r['cleared_rate']):.0%} "
                f"| {r['clear_med']} | {r['clear_max']} "
                f"| {float(r['first_run_up_rate']):.0%} "
                f"| {float(r['stayed_clear_rate']):.0%} "
                f"| {float(r['view_none_rate']):.0%} "
                f"| {r['record_advice']} |")

    # ---- S6: the route must age out in a stall and re-arm on relapse ----
    relapse = [r for r in extra if r["kind"] == "relapse"]
    if relapse:
        out.append("")
        out.append("Stall then relapse - a partial fix (one phase held at "
                   "0.35x baseline, clear of the 25% floor) for a full "
                   "10-run window, then a second regression at 0.10x. The "
                   "route must age out while the window is clear, stay "
                   "quiet, then re-escalate through rerun and reach fix "
                   "on the fitted bar for the second regression. `stall "
                   "clear` is the stall run the alert stops on (median); "
                   "`quiet` = no alert for the rest of the stall; `rerun` "
                   "/ `re-fix` are relapse runs; `ends fix` and `view fix` "
                   "are the final route and the /perfhistory verdict; "
                   "`--record` must stay 0:")
        out.append("| jitter | cleared | stall clear | quiet | rerun | "
                   "re-fix | ends fix | view fix | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for r in relapse:
            out.append(
                f"| {float(r['jitter']):.2f} "
                f"| {float(r['cleared_rate']):.0%} "
                f"| {r['stall_clear_med']} "
                f"| {float(r['quiet_rate']):.0%} "
                f"| {r['rerun_after_med']} | {r['refix_med']} "
                f"| {float(r['end_fix_rate']):.0%} "
                f"| {float(r['view_fix_rate']):.0%} "
                f"| {r['record_advice']} |")

    # ---- S7: stuck-alert escalation is evidence-gated ----
    stuck = [r for r in extra if r["kind"] == "stuck"]
    if stuck:
        out.append("")
        out.append("Stuck-alert escalation - a regression that lasts 24 runs, "
                   "deep (0.12x baseline) or marginal (0.22x, inside the "
                   "jitter to the 25% floor), then healthy runs. The "
                   "stuck wording may only appear once the phase has been "
                   "low for more than the whole window (11+ consecutive "
                   "runs): `escalated` is the share of timelines that "
                   "ever reach it; `onset` is the regression run it first "
                   "posts on (median); `early` counts an escalation "
                   "before that streak (must be 0); `up at end` is the "
                   "share still alerting when the fix lands; `clear at` / "
                   "`slowest` are healthy runs until it goes quiet ("
                   "trials alerting at the end only). `--record` stays 0:")
        out.append("| shape | jitter | escalated | onset | early | up at end "
                   "| clear at | slowest | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for r in stuck:
            out.append(
                f"| {r['shape']} | {float(r['jitter']):.2f} "
                f"| {float(r['escalated_rate']):.0%} | {r['onset_med']} "
                f"| {r['early']} | {float(r['alert_end_rate']):.0%} "
                f"| {r['clear_med']} | {r['clear_max']} "
                f"| {r['record_advice']} |")

    # ---- S8: the stuck alert across two hosts ----
    two_host = [r for r in extra if r["kind"] == "stuck_twohost"]
    if two_host:
        out.append("")
        out.append("Stuck alert across two hosts - a regression that runs "
                   "on host B only (deep 0.12x or marginal 0.22x of "
                   "baseline), interleaved 1:1 with a healthy host A, then "
                   "B recovers. The stuck wording must NAME B (`named A` "
                   "must be 0 - naming the healthy host would send the "
                   "operator to the wrong machine), may only escalate once "
                   "B's OWN streak passes the window (`early` must be 0), "
                   "and must clear once B's healthy runs return (`onset`, "
                   "`clear at` and `slowest` are in B's own runs; B's "
                   "window is its own last 10 runs at the 6-of-10 bar, so "
                   "5 healthy own runs clear it, the same arithmetic a "
                   "single machine gets). "
                   "`pooled` is the same count without host scoping: the "
                   "healthy host resets it every other run, so the pooled "
                   "gate could never escalate this case. `--record` stays "
                   "0:")
        out.append("| shape | jitter | escalated | named B | named A | onset "
                   "| early | pooled | up at end | clear at | slowest "
                   "| `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in two_host:
            out.append(
                f"| {r['shape']} | {float(r['jitter']):.2f} "
                f"| {float(r['escalated_rate']):.0%} "
                f"| {float(r['named_b_share']):.0%} "
                f"| {float(r['named_a_share']):.0%} "
                f"| {r['onset_med']} "
                f"| {r['early']} | {r['pooled_streak']} "
                f"| {float(r['alert_end_rate']):.0%} "
                f"| {r['clear_med']} | {r['clear_max']} "
                f"| {r['record_advice']} |")

    # ---- S9: a host-wide slide on one of two hosts ----
    shared = [r for r in extra if r["kind"] == "shared_twohost"]
    if shared:
        out.append("")
        out.append("Host-wide slide across two hosts - B slides EVERY phase "
                   "on its own runs (0.12x or 0.20x of baseline), "
                   "interleaved 1:1 with a healthy host A, then recovers. "
                   "`sustained` is the share whose line claims persistence "
                   "(\"have slid together\"); read POOLED, the same slide "
                   "is `pooled ev` (5 of 10) and sits below the 6-of-10 "
                   "bar, so `pooled sust` is 0% - the old rule called every "
                   "interleaved machine-wide slide transient. The control "
                   "is the same slide from a brand-new host: two own runs "
                   "(`own ev` 2/2) must stay transient. Both shapes route "
                   "to rule the host out (a machine is implicated either "
                   "way), never to one read path (`route fix` 0%), and the "
                   "line names B (`named A` must be 0). `clear at` is B's "
                   "own healthy runs until the warning stops; `--record` "
                   "stays 0:")
        out.append("| shape | depth | route host | route fix | sustained "
                   "| pooled sust | named B | named A | own ev | pooled ev "
                   "| clear at | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in shared:
            out.append(
                f"| {r['shape']} | {float(r['level']):.2f} "
                f"| {float(r['host_share']):.0%} "
                f"| {float(r['fix_share']):.0%} "
                f"| {float(r['sustained_share']):.0%} "
                f"| {float(r['pooled_sustained_share']):.0%} "
                f"| {float(r['named_b_share']):.0%} "
                f"| {float(r['named_a_share']):.0%} "
                f"| {r['own_evidence']} | {r['pooled_evidence']} "
                f"| {r['clear_med']} | {r['record_advice']} |")

    # ---- S10: the runs MOVE to another host mid-history ----
    switch = [r for r in extra if r["kind"] == "hostswitch"]
    if switch:
        out.append("")
        out.append("Host switch mid-history - host A has been stuck for 24 "
                   "of its own runs (long past saturation), then the runs "
                   "move to B. Two truths: B healthy -> nothing to act on "
                   "for B's first run (`clean 1st` 100%), and A's alert "
                   "may be reported but never re-posted as B's verdict "
                   "(`A leak` 0: the old code re-posted A's \"unresolved "
                   "REGRESSION\" on every clean B run and routed it to "
                   "\"fix the regressed read path\" on a machine that was "
                   "fine); B slow -> the stuck wording is EARNED on B's "
                   "own 11th low (`onset`, `early` 0) instead of inherited "
                   "from A's streak. `ctx A` is how many of the 18 "
                   "post-switch runs still carry A's separate alert (it "
                   "drops out of the recent-window candidates after 10 "
                   "runs elsewhere); `named B` is "
                   "the share of post-switch runs whose verdict names B; "
                   "`clear at` is B's healthy runs until nothing routes "
                   "(\"-\" when B never had an alert to clear). `--record` "
                   "stays 0:")
        out.append("| shape | clean 1st | A leak | stuck 1st | named B "
                   "| ctx A | onset | early | clear at | `--record` |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in switch:
            out.append(
                f"| {r['shape']} "
                f"| {float(r['clean_share']):.0%} "
                f"| {float(r['leak_share']):.0%} "
                f"| {float(r['stuck_share']):.0%} "
                f"| {float(r['named_b_share']):.0%} "
                f"| {r['context_med']} | {r['onset_med']} "
                f"| {r['early']} | {r['clear_med']} "
                f"| {r['record_advice']} |")

    # ---- S10b: the idle host comes back ----
    back = [r for r in extra if r["kind"] == "hostreturn"]
    if back:
        out.append("")
        out.append("Idle host returns - A is still stuck, the runs went to B "
                   "for 12 runs, then A runs once. A's window is its OWN "
                   "last 10 runs (`own ev` 10/10), so the saturated alert "
                   "is back on that first run (`alert back` 100%), naming A "
                   "and never B. The shared-window rule left `old slice` "
                   "(1) of A's runs inside the window, below "
                   "`HOST_MIN_SLICE`, so it had to wait for two more runs "
                   "before it could say anything at all. `--record` stays "
                   "0:")
        out.append("| shape | alert back | named A | named B | own ev "
                   "| old slice | `--record` |")
        out.append("|---|---|---|---|---|---|---|")
        for r in back:
            out.append(
                f"| {r['shape']} | {float(r['rate']):.0%} "
                f"| {float(r['named_a_share']):.0%} "
                f"| {float(r['named_b_share']):.0%} "
                f"| {r['own_evidence']} | {r['thin']} "
                f"| {r['record_advice']} |")

    # ---- S1: a second, ACTING host interleaved into one history ----
    acting = _pct(extra)
    if acting:
        out.append("")
        out.append("Two-host scoping - a second host takes every 2nd run "
                   "and is low on that share of ITS OWN runs (15/30/60/"
                   "100%); the fitted bar asks 6 of a host's own last 10, "
                   "so a flickering host is mostly not called. A fire "
                   "naming the healthy host is a FALSE fire, a fire naming "
                   "the slow host is a catch, `fires` is the share of "
                   "timelines that ever fire and `med B-runs` the slow "
                   "host's own runs until its first catch. The `pooled` "
                   "rows are the pre-host-metadata counterfactual:")
        out.append("| B acting | setting | slice view | fires | "
                   "named A (false) | named B (caught) | unlabeled | "
                   "med B-runs |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in acting:
            lat_med = r["lat_host_med"] or "-"
            out.append(
                f"| {float(r['level']):.0%} | `{r['label'].strip()}` "
                f"| {r['regime']} | {float(r['rate']):.0%} "
                f"| {r['named_a']} | {r['named_b']} "
                f"| {r['unlabeled']} | {lat_med} |")

    # ---- S2: a genuinely regressed host at a low share of the history
    # (does the healthy rest of the pool MASK it?) ----
    masked = _mask(extra)
    if masked:
        out.append("")
        out.append("Mask direction - a host regressed EVERY time it "
                   "runs, at a fraction of the history (truth: real "
                   "regression; zero catches here would be missed ones; "
                   "latency in total runs and in the regressed host's "
                   "own runs):")
        out.append("| A share | setting | slice view | fires | "
                   "med total runs | med A's own runs | missed |")
        out.append("|---|---|---|---|---|---|---|")
        for r in masked:
            lat_total = r["lat_total_med"] or "-"
            lat_host = r["lat_host_med"] or "-"
            out.append(
                f"| {float(r['level']):.0%} | `{r['label'].strip()}` "
                f"| {r['regime']} | {float(r['rate']):.0%} "
                f"| {lat_total} | {lat_host} "
                f"| {r['missed']} |")

    out.append("<!-- end stress-matrix -->")
    return "\n".join(out)


def check(csv_path, doc_path=DEFAULT_DOC):
    """(ok, message): does the doc's generated block match this CSV?

    Line endings are normalised (the doc is CRLF on Windows checkouts)
    but nothing else is: the numbers must come from the renderer, not
    from a hand-edit. A stale block reports the first divergent lines
    and the exact regeneration commands.
    """
    ff, lat, rows = load(csv_path)
    expected = render(ff, lat, rows).replace("\r\n", "\n").rstrip("\n")
    try:
        doc = pathlib.Path(doc_path).read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read {doc_path}: {exc}"
    if BLOCK_START not in doc or BLOCK_END not in doc:
        return False, (f"{doc_path} has no '{BLOCK_START} ... "
                       f"{BLOCK_END}' block to check")
    start = doc.index(BLOCK_START)
    end = doc.index(BLOCK_END, start) + len(BLOCK_END)
    current = doc[start:end].replace("\r\n", "\n").rstrip("\n")
    if current == expected:
        return True, f"stress-matrix block in {doc_path} is up to date"
    diff = list(difflib.unified_diff(
        current.split("\n"), expected.split("\n"),
        "committed block", "rendered from " + str(csv_path),
        lineterm="", n=0))
    return False, (
        "stress-matrix block is STALE - the doc no longer matches a "
        "measured run. Regenerate it:\n"
        "  py scripts/trend_stress_test.py --refit --csv /tmp/trend.csv\n"
        "  py scripts/render_stress_matrix.py /tmp/trend.csv > /tmp/matrix.md\n"
        "then replace the block between the markers. First diffs:\n"
        + "\n".join(diff[:20])
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render (or --check) the CONTRIBUTING stress matrix")
    ap.add_argument("csv", help="CSV written by trend_stress_test.py --csv")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when the doc's generated block is stale")
    ap.add_argument("--doc", default=str(DEFAULT_DOC),
                    help=f"doc carrying the block (default: {DEFAULT_DOC})")
    args = ap.parse_args(argv)
    if args.check:
        ok, message = check(args.csv, args.doc)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    ff, lat, rows = load(args.csv)
    print(render(ff, lat, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
