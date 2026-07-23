import sqlite3

DATABASE = "scoutbot.db"


def create_database():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        minimum_visits INTEGER DEFAULT 100000,
        minimum_players INTEGER DEFAULT 100,
        genre TEXT DEFAULT 'Any'
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


def update_user(discord_id, minimum_visits=None, minimum_players=None, genre=None):
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
    if genre is not None:
        fields.append("genre = ?")
        values.append(genre)

    if fields:
        query = f"UPDATE users SET {', '.join(fields)} WHERE discord_id = ?"
        values.append(discord_id)
        cursor.execute(query, values)
        conn.commit()

    conn.close()
