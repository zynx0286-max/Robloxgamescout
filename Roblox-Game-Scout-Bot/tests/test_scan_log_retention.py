"""scan_log retention: the one table that used to grow unbounded.

scan_log gets several rows per scan cycle for the life of the bot. These
tests pin the contract introduced with 30-day retention:

  * stale rows are pruned opportunistically (at most every few hours);
  * recent rows — and the row being written — always survive;
  * a failed prune never loses the insert.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import database


def _db(tmp_path, monkeypatch) -> str:
    db_path = str(tmp_path / "scanlog.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    database.create_database()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT event, message FROM scan_log ORDER BY timestamp"
    ).fetchall()
    conn.close()
    return rows


def _reset_prune_clock():
    database._last_scan_log_prune = None


def test_stale_scan_log_rows_are_pruned_on_write(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _reset_prune_clock()
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(sep=" ")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO scan_log (event, message, timestamp) VALUES (?, ?, ?)",
        ("cycle_summary", "ancient", old),
    )
    conn.commit()
    conn.close()

    database.save_alert_log("cycle_summary", "fresh write")

    rows = _rows(db)
    assert ("cycle_summary", "ancient") not in rows
    assert ("cycle_summary", "fresh write") in rows


def test_recent_rows_survive_the_prune(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _reset_prune_clock()
    database.save_alert_log("scan_complete", "yesterday-ish")
    _reset_prune_clock()  # force the next write to be due
    database.save_alert_log("cycle_summary", "today")

    rows = _rows(db)
    assert ("scan_complete", "yesterday-ish") in rows
    assert ("cycle_summary", "today") in rows


def test_prune_is_rate_limited_and_never_loses_the_insert(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _reset_prune_clock()
    database.save_alert_log("cycle_summary", "first")
    # Second write moments later: the prune is not due again yet, but the
    # insert must still land.
    database._last_scan_log_prune = datetime.now(timezone.utc)
    database.save_alert_log("cycle_summary", "second")

    rows = _rows(db)
    assert ("cycle_summary", "first") in rows
    assert ("cycle_summary", "second") in rows


def test_failed_prune_still_commits_the_insert(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _reset_prune_clock()

    real_connect = database._connect

    class _BrokenPruneConn:
        """Commit works; the DELETE raises, like a locked WAL checkpoint."""

        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            inner_cursor = self._inner.cursor()

            class _Cursor:
                def execute(self, sql, *a):
                    if sql.lstrip().upper().startswith("DELETE"):
                        raise sqlite3.OperationalError("database is locked")
                    return inner_cursor.execute(sql, *a)

                def __getattr__(self, name):
                    return getattr(inner_cursor, name)

            return _Cursor()

        def commit(self):
            self._inner.commit()

        def close(self):
            self._inner.close()

    monkeypatch.setattr(
        database, "_connect", lambda: _BrokenPruneConn(real_connect())
    )

    database.save_alert_log("scan_complete", "must survive")
    assert ("scan_complete", "must survive") in _rows(db)
