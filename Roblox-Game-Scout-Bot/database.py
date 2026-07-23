import sqlite3

DATABASE = "scoutbot.db"


def _add_column_if_not_exists(cursor, table, column, definition):
    """Add a column to a table if it doesn't already exist."""
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_database():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        minimum_visits INTEGER DEFAULT 100000,
        minimum_players INTEGER DEFAULT 100,
        minimum_growth INTEGER DEFAULT 0,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 365
    )
    """)

    # Add missing columns for older databases
    _add_column_if_not_exists(cursor, "users", "minimum_growth", "INTEGER DEFAULT 0")
    _add_column_if_not_exists(cursor, "users", "genre", "TEXT DEFAULT 'Any'")
    _add_column_if_not_exists(cursor, "users", "max_age", "INTEGER DEFAULT 365")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER,
        players INTEGER,
        visits INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


def add_user(discord_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT OR IGNORE INTO users (discord_id)
    VALUES (?)
    """, (discord_id,))

    conn.commit()
    conn.close()


def get_user(discord_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT * FROM users WHERE discord_id = ?
    """, (discord_id,))

    user = cursor.fetchone()

    conn.close()

    return user


def update_user(
    discord_id,
    minimum_visits=None,
    minimum_players=None,
    minimum_growth=None,
    genre=None,
    max_age=None,
):
    """Update user settings. Only updates fields that are provided."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    fields = []
    values = []

    if minimum_visits is not None:
        fields.append("minimum_visits = ?")
        values.append(minimum_visits)
    if minimum_players is not None:
        fields.append("minimum_players = ?")
        values.append(minimum_players)
    if minimum_growth is not None:
        fields.append("minimum_growth = ?")
        values.append(minimum_growth)
    if genre is not None:
        fields.append("genre = ?")
        values.append(genre)
    if max_age is not None:
        fields.append("max_age = ?")
        values.append(max_age)

    if fields:
        query = f"UPDATE users SET {', '.join(fields)} WHERE discord_id = ?"
        values.append(discord_id)
        cursor.execute(query, values)
        conn.commit()

    conn.close()


def save_game_snapshot(game):
    """Save a snapshot of a game's stats to the history table."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO game_history
    (game_id, players, visits)
    VALUES (?, ?, ?)
    """,
    (
        game["id"],
        game["playing"],
        game["visits"]
    ))

    conn.commit()
    conn.close()
