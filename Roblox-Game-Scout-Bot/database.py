import sqlite3

DATABASE = "scoutbot.db"


def _table_columns(cursor, table):
    """Return a list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _recreate_users_table(cursor):
    """Drop and recreate the users table with the full schema."""
    cursor.execute("DROP TABLE IF EXISTS users")
    cursor.execute("""
    CREATE TABLE users (
        discord_id INTEGER PRIMARY KEY,
        minimum_visits INTEGER DEFAULT 100000,
        minimum_players INTEGER DEFAULT 100,
        minimum_growth INTEGER DEFAULT 0,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 365
    )
    """)


def create_database():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    expected_columns = {
        "discord_id",
        "minimum_visits",
        "minimum_players",
        "minimum_growth",
        "genre",
        "max_age",
    }

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

    existing_columns = set(_table_columns(cursor, "users"))
    if not expected_columns.issubset(existing_columns):
        _recreate_users_table(cursor)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER,
        players INTEGER,
        visits INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saved_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game_id INTEGER NOT NULL,
        game_name TEXT NOT NULL,
        date_saved TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, game_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS alerted_games (
        game_id INTEGER PRIMARY KEY,
        game_name TEXT,
        first_alerted TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL,
        message TEXT,
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
        values.append(int(minimum_visits))
    if minimum_players is not None:
        fields.append("minimum_players = ?")
        values.append(int(minimum_players))
    if minimum_growth is not None:
        fields.append("minimum_growth = ?")
        values.append(int(minimum_growth))
    if genre is not None:
        fields.append("genre = ?")
        values.append(genre)
    if max_age is not None:
        fields.append("max_age = ?")
        values.append(int(max_age))

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


def save_game_for_user(user_id, game):
    """Save a game to a user's watchlist."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO saved_games (user_id, game_id, game_name)
    VALUES (?, ?, ?)
    ON CONFLICT(user_id, game_id) DO UPDATE SET
        game_name = excluded.game_name,
        date_saved = CURRENT_TIMESTAMP
    """,
    (
        user_id,
        game["id"],
        game["name"]
    ))

    conn.commit()
    conn.close()


def get_saved_games_for_user(user_id):
    """Return all games saved by a user."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT game_id, game_name, date_saved
    FROM saved_games
    WHERE user_id = ?
    ORDER BY date_saved DESC
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def is_alerted(game_id):
    """Return True if this Universe ID has already been alerted on."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM alerted_games WHERE game_id = ?",
        (game_id,),
    )
    exists = cursor.fetchone() is not None

    conn.close()
    return exists


def mark_alerted(game_id, game_name):
    """Record that this Universe ID has been alerted on."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT OR IGNORE INTO alerted_games (game_id, game_name)
    VALUES (?, ?)
    """, (game_id, game_name))

    conn.commit()
    conn.close()


def save_alert_log(event, message):
    """Append a single row to the scan_log table."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO scan_log (event, message)
    VALUES (?, ?)
    """, (event, message))

    conn.commit()
    conn.close()
