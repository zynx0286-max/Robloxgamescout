"""Tests for the /benchmark command.

Same light-touch contract as the other command tests: /benchmark must be
registered with the real data path wired, and its implementation must
run the bench in a SUBPROCESS (the bench mutates the module-level
database.DATABASE to point at a throwaway store — doing that in-process
would momentarily redirect a live scan).
"""

import os
import pathlib
import subprocess
import sys


def _project_root():
    return pathlib.Path(__file__).resolve().parents[1]


def test_benchmark_command_is_registered():
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert 'name="benchmark"' in source
    assert "bench_bulk_reads.py" in source  # real data path wired
    assert "create_subprocess_exec" in source  # never in-process


def test_bench_script_runs_standalone_and_prints_speedup(tmp_path):
    """The subprocess /benchmark posts from must work with no bot present.

    This is the command's data path end-to-end minus Discord: fresh
    interpreter, throwaway store, deterministic output format. The
    perf-history entry lands in a TEMP file (PERF_HISTORY_PATH env):
    this suite runs the bench for real, and without the redirect every
    pytest run would append to the committed data/perf_history.jsonl —
    including CI, where the file is not executable evidence anyway.
    """
    import json

    script = _project_root() / "scripts" / "bench_bulk_reads.py"
    history_file = tmp_path / "history.jsonl"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(_project_root()),
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PERF_HISTORY_PATH": str(history_file)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "speedup:" in proc.stdout
    # Format the command posts: "old ... X s", "bulk ... Y s", "speedup: Zx"
    assert "old (per-game" in proc.stdout
    assert "bulk (one query" in proc.stdout
    # the history side effect went to the temp file, with a real entry
    assert history_file.exists()
    entry = json.loads(history_file.read_text(encoding="utf-8").splitlines()[0])
    assert entry["recorded"] is False
    assert "growth" in entry["phases"]
    assert "history: logged to" in proc.stdout


def test_benchmark_posts_the_per_machine_table():
    """/benchmark measures ONE host, so the other machines' verdicts can
    only reach the channel through the shared table renderer — the same
    one /perfhistory embeds."""
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert "bench_machine_lines" in source


def test_machine_table_bridge_is_quiet_for_one_host(tmp_path, monkeypatch):
    """One machine = no table (the run's own lines say everything); a
    second machine in the log earns a row each."""
    import json

    import bench_guard

    history = tmp_path / "history.jsonl"
    win = {"python": "3.14.7", "platform": "Windows-11-10.0.26200-SP0"}
    lin = {"python": "3.12.10", "platform": "Linux-6.8.0-1012-aws"}
    rows = [{"ts": f"2026-10-20T0{i}:00:00+00:00",
             "phases": {"growth": 190.0}, "host": win} for i in range(3)]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                       encoding="utf-8")
    monkeypatch.setenv("PERF_HISTORY_PATH", str(history))
    assert bench_guard.bench_machine_lines() == []

    rows.append({"ts": "2026-10-20T05:00:00+00:00",
                 "phases": {"growth": 190.0}, "host": lin})
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                       encoding="utf-8")
    lines = bench_guard.bench_machine_lines()
    assert lines[0].startswith("per machine")
    assert any("Windows/py3.14.7" in ln for ln in lines)
    assert any("Linux/py3.12.10" in ln for ln in lines)


def test_perfhistory_command_is_registered_and_read_only():
    import inspect

    import commands as commands_mod

    source = inspect.getsource(commands_mod.setup_commands)
    assert 'name="perfhistory"' in source
    assert "bench_read_history_view" in source  # the read-only bridge
    # the point of the command: no bench subprocess, no re-measuring
    assert "create_subprocess_exec" not in source.split(
        "def perfhistory")[1].split("@tree.command(name=\"about\"")[0]
