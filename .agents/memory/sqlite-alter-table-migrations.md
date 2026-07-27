---
name: SQLite in-place migrations
description: How to safely add columns to a SQLite table from Python when the function also recreates the table.
---

In `create_database()`, a pattern like this fails the second time it runs:

```python
existing = set(_table_columns(cursor, "users"))
if not expected.issubset(existing):
    _recreate_users_table(cursor)   # DROPs + CREATEs with full schema
if "max_age" not in existing:      # ← stale snapshot!
    cursor.execute("ALTER TABLE users ADD COLUMN max_age ...")
```

**Why:** `_table_columns()` ran against the OLD table before `_recreate_users_table()` rebuilt it, so the variable still says "missing" even though the column now exists. ALTER TABLE then errors with `duplicate column name`.

**How to apply:** after any create-or-recreate path, re-fetch the column set:

```python
if not expected.issubset(existing):
    _recreate_users_table(cursor)
    existing = set(_table_columns(cursor, "users"))   # refresh
if "max_age" not in existing:
    cursor.execute("ALTER TABLE users ADD COLUMN max_age ...")
```

Rule of thumb: anything that runs `CREATE`/`REPLACE` mutates `PRAGMA table_info` results — read it again before deciding on an `ALTER TABLE ADD COLUMN`.
