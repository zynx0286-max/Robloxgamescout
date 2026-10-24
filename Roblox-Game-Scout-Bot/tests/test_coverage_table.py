"""Tests for scripts/coverage_table.py — pure functions, synthetic XML.

The generator's contract: production modules only, weakest first, low
coverage starred, tests/scripts scaffolding filtered out, and a stable
render (same XML -> same text) so --check can guard staleness in review.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import coverage_table as ct


def _xml(tmp_path, classes):
    """Write a minimal coverage.py-compatible XML with the given classes."""
    entries = "\n".join(
        f'<class filename="{name}">'
        f'<summary statements="{stmts}" missing="{miss}"/></class>'
        for name, stmts, miss in classes
    )
    path = tmp_path / "coverage.xml"
    path.write_text(f"<coverage><classes>{entries}</classes></coverage>", encoding="utf-8")
    return path


def _module_order(table):
    return [
        line.split(" | ")[0].lstrip("| ").split(" ⚠️")[0]
        for line in table.splitlines()
        if line.startswith(("| filters", "| pulse", "| scanner"))
    ]


def test_production_modules_sorted_weakest_first(tmp_path):
    path = _xml(tmp_path, [
        ("scanner.py", 100, 10),   # 90%
        ("filters.py", 100, 80),   # 20%
        ("pulse.py", 50, 30),      # 40%
    ])
    assert _module_order(ct.build_table(path)) == ["filters.py", "pulse.py", "scanner.py"]


def test_weak_files_get_the_alert_star(tmp_path):
    path = _xml(tmp_path, [("filters.py", 100, 80), ("scanner.py", 100, 10)])
    table = ct.build_table(path)
    assert "| filters.py ⚠️ |" in table
    assert "| scanner.py |" in table
    assert "below 50% line coverage" in table  # the legend that explains the star


def test_tests_and_scripts_are_excluded(tmp_path):
    path = _xml(tmp_path, [
        ("tests/test_pulse.py", 100, 0),
        ("scripts/bench_bulk_reads.py", 100, 0),
        ("database.py", 100, 0),
        ("conftest.py", 100, 0),
    ])
    table = ct.build_table(path)
    assert "test_pulse.py" not in table
    assert "bench_bulk_reads.py" not in table
    assert "conftest.py" not in table
    assert "database.py | 100 | 0 | 100%" in table


def test_multi_class_files_are_aggregated(tmp_path):
    path = _xml(tmp_path, [("history.py", 60, 30), ("history.py", 40, 10)])
    stmts, missing, rate = ct._class_rows(path)["history.py"]
    assert (stmts, missing) == (100, 40)
    assert rate == 60.0


def test_upsert_idempotent_and_stale_table_is_detected(tmp_path):
    md = tmp_path / "ARCHITECTURE.md"
    md.write_text("# Doc\n", encoding="utf-8")
    xml = _xml(tmp_path, [("history.py", 100, 50)])

    assert ct.upsert_architecture(xml, md) is True   # first pass creates the block
    assert ct.upsert_architecture(xml, md) is False  # second pass: already current

    xml2 = _xml(tmp_path, [("history.py", 100, 10)])
    fresh = ct.build_table(xml2)
    text = md.read_text(encoding="utf-8")
    start = text.index(ct.START_MARKER)
    end = text.index(ct.END_MARKER, start) + len(ct.END_MARKER)
    assert text[start + len(ct.START_MARKER):end - len(ct.END_MARKER)].strip() != fresh

    ct.upsert_architecture(xml2, md)
    assert ct.upsert_architecture(xml2, md) is False


def test_marker_block_is_upserted_in_place_with_footer_intact(tmp_path):
    md = tmp_path / "ARCHITECTURE.md"
    md.write_text("# Doc\n\n-- footer --\n", encoding="utf-8")

    xml = _xml(tmp_path, [("history.py", 100, 50)])
    ct.upsert_architecture(xml, md)
    xml2 = _xml(tmp_path, [("history.py", 100, 50), ("pulse.py", 100, 60)])
    ct.upsert_architecture(xml2, md)  # grew — must replace the old block only

    text = md.read_text(encoding="utf-8")
    assert text.count(ct.START_MARKER) == 1
    assert text.count(ct.END_MARKER) == 1
    assert text.startswith("# Doc\n\n-- footer --")  # footer untouched
    assert text.rstrip().endswith(ct.END_MARKER)      # block appended at the end
    assert "pulse.py ⚠️" in text   # 40% < 50 → starred
    assert "| history.py |" in text  # exactly 50% is NOT below the threshold
    # first upsert appends at the end (no markers existed yet): the
    # pre-existing footer must survive, positioned before the new block
    assert text.index("-- footer --") < text.index(ct.START_MARKER)
