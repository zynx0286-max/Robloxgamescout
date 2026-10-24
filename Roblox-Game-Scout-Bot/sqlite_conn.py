"""One place for the SQLite connection contract.

Every connection to the shared SQLite file (bot tables, portfolio tables,
AI caches — all in one file) applies the same per-connection settings
through `tune()`:

* `busy_timeout` (30 s) — WAL lets readers and one writer coexist, but a
  burst of writers still queues on the database lock; waiting is correct,
  failing after 30 s is better than hanging forever.
* `synchronous=NORMAL` — WAL-mode crash safety at a fraction of the FULL
  fsync cost. NORMAL guarantees a consistent database across app crashes,
  which is the failure mode a bot restart actually imposes.
* `temp_store=MEMORY` — keeps sort spills (leaderboard ordering, bulk
  GROUP BYs) off disk.

Conversely, WAL itself (`journal_mode=WAL`) is a *file* property, set
once at `database.create_database()`; it must NOT be set per connection
(re-enabling it takes a write lock on every connect and contends with
actual work).

This module never opens connections itself for the hot paths — modules
keep their own `_connect()` helpers that call their module-local
`sqlite3.connect` and then `tune()`. That layout keeps the settings in
one source of truth while each module stays individually importable and
individually monkeypatchable in tests.
"""

SQLITE_BUSY_TIMEOUT_SECONDS = 30


def tune(conn) -> None:
    """Apply the standard per-connection pragma set. Best-effort: a
    read-only or exotic filesystem that refuses pragmas falls back to
    SQLite defaults, never to an exception."""
    import sqlite3

    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        pass


def connect(path, **kwargs):
    """Convenience for new code: connect + tune in one call.

    Hot-path modules keep their own module-local `_connect()` helpers
    (so tests can patch per module); this is for callers that don't
    have one yet.
    """
    import sqlite3

    conn = sqlite3.connect(path, **kwargs)
    tune(conn)
    return conn
