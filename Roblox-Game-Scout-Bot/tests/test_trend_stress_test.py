"""Smoke tests: the trend-stress harness runs offline, writes nothing
(except the explicit --csv file), and its CSV renders into the
CONTRIBUTING matrix block via scripts/render_stress_matrix.py. Tiny
parameters keep it cheap in CI; deterministic seeds make failing
output reproducible. The clustered noise bands are AR(1) (the model of
record) and `--refit` re-derives the threshold policy from them.
"""

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "trend_stress_test.py"
RENDER = ROOT / "scripts" / "render_stress_matrix.py"


def _run(args, timeout=300):
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True,
        timeout=timeout, cwd=str(ROOT),
    )


def test_stress_harness_smoke_and_csv(tmp_path):
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "6", "--steps", "40",
                 "--latency-trials", "3", "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = proc.stdout
    assert "iid noise" in out                             # memoryless model
    assert "AR(1) cluster noise" in out                   # clustered model
    assert "rho = 0.8" in out and "rho = 0.5" in out      # both persistencess
    assert "bursty noise" not in out                      # retired model, off
    assert "w10 scaled thr6" in out and "CURRENT" in out  # fitted default
    assert "w10 fixed thr4" in out                        # previous policy
    assert "detection latency" in out                     # cost side
    assert "S3 operator routing" in out                   # fix-vs-rerun matrix
    assert "S7 stuck-alert escalation" in out             # streak-gated alert
    assert "S8 stuck alert across two hosts" in out       # named per host
    assert "S9 host-wide slide across two hosts" in out   # shared slide
    assert "S10 host switch" in out                       # verdict follows
    assert "S10b idle host returns" in out                # own window kept
    assert "CSV written" in out and str(csv_path) in out

    text = csv_path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("kind,"), text.splitlines()[0]
    assert "falsefire" in text and "latency" in text
    assert "w10 scaled thr6" in text
    assert "ar1" in text
    assert "routing" in text
    assert "stuck" in text and "stuck_twohost" in text
    assert "shared_twohost" in text
    assert "hostswitch" in text and "hostreturn" in text


def test_render_matrix_from_csv(tmp_path):
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "4", "--steps", "30",
                 "--latency-trials", "2", "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    rendered = _run([str(RENDER), str(csv_path)])
    assert rendered.returncode == 0, rendered.stderr
    block = rendered.stdout
    assert "GENERATED" in block and "<!-- end stress-matrix -->" in block
    assert block.startswith("<!-- stress-matrix:")  # leading marker
    assert "| noise |" in block and "|---|" in block
    assert "w10 scaled thr6" in block
    assert "ar1 rho0.8 0.1" in block               # AR(1) band labels
    assert "Operator routing" in block             # fix-vs-rerun table
    assert "Stuck-alert escalation" in block       # evidence-gated escalation
    assert "| shape | jitter | escalated |" in block
    assert "Stuck alert across two hosts" in block  # named, per-host streak
    assert "| pooled |" in block
    assert "Host-wide slide across two hosts" in block
    assert "| sustained |" in block
    assert "Host switch mid-history" in block        # verdict on this run
    assert "| A leak |" in block
    assert "Idle host returns" in block              # per-host window
    assert "Detection latency" in block


def test_refit_grid_and_render(tmp_path):
    """--refit grid-searches the policy and its rows render as a table:
    the production policy must appear in the grid (whatever the tiny run
    happens to fit), and the CSV carries the refit kind."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "3", "--steps", "20",
                 "--latency-trials", "1", "--refit", "--fit-trials", "3",
                 "--fit-steps", "40", "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = proc.stdout
    assert "threshold re-fit under AR(1)" in out
    assert "fitted best:" in out and "production:" in out
    # the production row's own score is always printed
    assert "0.60" in out

    text = csv_path.read_text(encoding="utf-8")
    assert "refit" in text

    rendered = _run([str(RENDER), str(csv_path)])
    assert rendered.returncode == 0, rendered.stderr
    block = rendered.stdout
    assert "Policy re-fit under the AR(1) model" in block
    assert "| window | share | thr |" in block
    assert "**current**" in block


def test_stuck_rows_never_escalate_early_or_recommend_recording(tmp_path):
    """The S7 rows must never report an escalation before the streak is
    real nor advice to re-record, whatever jitter or depth: those two
    columns are the whole point of the probe, so they are asserted on
    the measured rows rather than only on the rendered text."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "2",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["kind"] == "stuck"]
    assert {r["shape"] for r in rows} == {"deep", "marginal"}
    assert {r["jitter"] for r in rows} == {"0.00", "0.10", "0.15", "0.25"}
    for r in rows:
        label = (r["shape"], r["jitter"])
        assert r["early"] == "0", label          # chain of custody for the alert
        assert r["record_advice"] == "0", label  # a regression is never re-recorded
        if r["onset_med"] != "-":
            assert int(r["onset_med"]) >= 11, label  # 10-window + 1
        if r["clear_med"] != "-":
            assert 1 <= int(r["clear_med"]) <= 10, label
            assert int(r["clear_max"]) >= int(r["clear_med"]), label
    deep = [r for r in rows if r["shape"] == "deep"]
    assert all(float(r["escalated_rate"]) == 1.0 for r in deep), deep


def test_two_host_stuck_rows_name_the_regressed_host(tmp_path):
    """S8 rows: the stuck line must name host B (a line naming the
    healthy host is misattribution), never escalate before B's own
    streak is real, and never recommend re-recording. Deep regressions
    must escalate in every timeline."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "3",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if r["kind"] == "stuck_twohost"]
    assert {r["shape"] for r in rows} == {"deep", "marginal"}
    for r in rows:
        label = (r["shape"], r["jitter"])
        assert r["early"] == "0", label
        assert r["record_advice"] == "0", label
        assert float(r["named_a_share"]) == 0.0, label
        assert r["pooled_streak"] == "1", label   # reset by the healthy host
        assert int(r["onset_med"]) >= 11, label
    deep = [r for r in rows if r["shape"] == "deep"]
    assert all(float(r["escalated_rate"]) == 1.0 for r in deep), deep
    assert all(float(r["named_b_share"]) == 1.0 for r in deep), deep


def test_shared_slide_rows_separate_own_evidence_from_pooled(tmp_path):
    """S9 rows: the interleaved machine-wide slide must claim persistence
    (route to the host, never one read path), while the same slide from
    a two-run host must stay transient; the pooled column shows why the
    old rule called both transient."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "3",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if r["kind"] == "shared_twohost"]
    assert {r["shape"] for r in rows} == {"sustained", "thin"}
    for r in rows:
        label = (r["shape"], r["level"])
        assert r["host_share"] == "1.0000", label  # rule the host out
        assert r["fix_share"] == "0.0000", label   # never one read path
        assert r["named_a_share"] == "0.0000", label
        assert r["record_advice"] == "0", label
        assert float(r["pooled_sustained_share"]) == 0.0, label
        if r["shape"] == "sustained":
            assert r["sustained_share"] == "1.0000", label
            # its OWN last 10 runs, not the 5 a shared window leaves it
            assert r["own_evidence"] == "10/10", label
            assert r["pooled_evidence"] == "5/10", label
            assert r["clear_med"] == "5", label
        else:
            assert r["sustained_share"] == "0.0000", label
            assert r["own_evidence"] == "2/2", label
            assert r["clear_med"] == "-", label


def test_idle_host_returns_to_its_own_window(tmp_path):
    """S10b rows: after 12 runs on B, A's first run back must carry its
    saturated alert straight away (its own 10-run window is intact),
    naming A and never B - the shared window had only 1 of A's runs left
    and could not speak until A had run twice more."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "3",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["kind"] == "hostreturn"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["rate"] == "1.0000", row         # alert back on the 1st run
    assert row["named_a_share"] == "1.0000", row
    assert row["named_b_share"] == "0.0000", row
    assert row["own_evidence"] == "10/10", row
    assert row["thin"] == "1", row              # what the shared window had
    assert row["record_advice"] == "0", row


def test_host_switch_rows_keep_the_verdict_on_the_running_host(tmp_path):
    """S10 rows: after the runs move to host B, A's stuck alert may be
    reported but must never become B's verdict (`A leak` 0) and never
    fire a stuck line on B's first run; a slow B earns its own stuck
    alert at 11 of its own runs and nothing recommends re-recording."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "3",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr or proc.stdout

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if r["kind"] == "hostswitch"]
    assert {r["shape"] for r in rows} == {"fixed by switch", "new host slow"}
    for r in rows:
        label = r["shape"]
        assert r["leak_share"] == "0.0000", label   # never A's verdict
        assert r["stuck_share"] == "0.0000", label  # never inherited
        assert r["early"] == "0", label
        assert r["record_advice"] == "0", label
        assert int(r["context_med"]) > 0, label     # A is reported, not dropped
    fixed = next(r for r in rows if r["shape"] == "fixed by switch")
    assert fixed["clean_share"] == "1.0000", fixed     # healthy B: no action
    assert fixed["named_b_share"] == "0.0000", fixed
    assert fixed["onset_med"] == "-", fixed
    assert fixed["clear_med"] == "-", fixed     # nothing to clear on arrival
    slow = next(r for r in rows if r["shape"] == "new host slow")
    assert slow["clean_share"] == "0.0000", slow       # its own dip is real
    assert float(slow["named_b_share"]) > 0.5, slow
    assert int(slow["onset_med"]) >= 11, slow   # earned on B's own streak
    assert 1 <= int(slow["clear_med"]) <= 10, slow


def test_check_mode_passes_fresh_and_fails_stale(tmp_path):
    """The review gate: a block rendered from this CSV passes, a single
    mutated cell fails with the regeneration commands."""
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "1",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr
    block = _run([str(RENDER), str(csv_path)])
    assert block.returncode == 0, block.stderr

    doc = tmp_path / "CONTRIBUTING.md"
    doc.write_text("# Title\r\n\r\n" + block.stdout.replace("\n", "\r\n")
                   + "\r\n\r\nTail\r\n", encoding="utf-8", newline="")
    fresh = _run([str(RENDER), str(csv_path), "--check",
                  "--doc", str(doc)])
    assert fresh.returncode == 0, fresh.stderr
    assert "is up to date" in fresh.stdout

    text = doc.read_text(encoding="utf-8")
    start = text.index("<!-- stress-matrix:")
    stale_text = (text[:start]
                  + text[start:].replace("0%", "7%", 1))
    stale_doc = tmp_path / "STALE.md"
    stale_doc.write_text(stale_text, encoding="utf-8", newline="")
    stale = _run([str(RENDER), str(csv_path), "--check",
                  "--doc", str(stale_doc)])
    assert stale.returncode == 1
    out = stale.stderr or stale.stdout
    assert "STALE" in out
    assert "trend_stress_test.py --refit" in out   # how to fix it
    assert "committed block" in out                # where it diverged


def test_check_mode_requires_the_marker_block(tmp_path):
    csv_path = tmp_path / "trend.csv"
    proc = _run([str(SCRIPT), "--trials", "2", "--steps", "20",
                 "--latency-trials", "1", "--routing-trials", "1",
                 "--csv", str(csv_path)])
    assert proc.returncode == 0, proc.stderr
    doc = tmp_path / "NONE.md"
    doc.write_text("no generated block here", encoding="utf-8")
    res = _run([str(RENDER), str(csv_path), "--check", "--doc", str(doc)])
    assert res.returncode == 1
    assert "has no" in (res.stderr or res.stdout)


def test_render_matrix_rejects_non_csv(tmp_path):
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n", encoding="utf-8")
    rendered = _run([str(RENDER), str(junk)])
    assert rendered.returncode != 0
    assert "trend-stress CSV" in (rendered.stderr or rendered.stdout)
