import sqlite3

from config import DATABASE_PATH

DATABASE = DATABASE_PATH


def _connect(path):
    """Open the shared SQLite file under the sqlite_conn contract."""
    import sqlite_conn

    conn = sqlite3.connect(path)
    sqlite_conn.tune(conn)
    return conn


def get_growth(game_id):
    """Return player growth percentage between the last two snapshots."""
    conn = _connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT players
    FROM game_history
    WHERE game_id = ?
    ORDER BY timestamp DESC
    LIMIT 2
    """,
    (game_id,))

    data = cursor.fetchall()

    conn.close()

    if len(data) < 2:
        return 0

    current = data[0][0]
    previous = data[1][0]

    if previous == 0:
        return 0

    growth = (
        (current - previous)
        / previous
    ) * 100

    return round(growth, 2)
