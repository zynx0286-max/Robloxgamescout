import sqlite3
from config import DATABASE_PATH

DATABASE = DATABASE_PATH


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
        minimum_visits INTEGER DEFAULT 0,
        maximum_visits INTEGER DEFAULT 1500000,
        minimum_players INTEGER DEFAULT 15,
        maximum_players INTEGER DEFAULT 2500,
        minimum_growth INTEGER DEFAULT 0,
        minimum_rating INTEGER DEFAULT 75,
        require_discord INTEGER DEFAULT 1,
        require_rotrends INTEGER DEFAULT 1,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 365,
        alert_level TEXT DEFAULT 'all'
    )
    """)


def create_database():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    expected_columns = {
        "discord_id",
        "minimum_visits",
        "maximum_visits",
        "minimum_players",
        "maximum_players",
        "minimum_growth",
        "minimum_rating",
        "require_discord",
        "require_rotrends",
        "genre",
        "max_age",
        "alert_level",
    }

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        minimum_visits INTEGER DEFAULT 0,
        maximum_visits INTEGER DEFAULT 1500000,
        minimum_players INTEGER DEFAULT 15,
        maximum_players INTEGER DEFAULT 2500,
        minimum_growth INTEGER DEFAULT 0,
        minimum_rating INTEGER DEFAULT 75,
        require_discord INTEGER DEFAULT 1,
        require_rotrends INTEGER DEFAULT 1,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 365,
        alert_level TEXT DEFAULT 'all'
    )
    """)

    existing_columns = set(_table_columns(cursor, "users"))
    if not expected_columns.issubset(existing_columns):
        _recreate_users_table(cursor)
        # Re-fetch columns after recreate — stale snapshot would cause
        # duplicate-column errors on the in-place ALTER TABLE below.
        existing_columns = set(_table_columns(cursor, "users"))

    # In-place migrations for users tables predating the Smart Filters columns.
    if "maximum_visits" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN maximum_visits INTEGER DEFAULT 1500000")
    if "maximum_players" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN maximum_players INTEGER DEFAULT 2500")
    if "minimum_rating" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN minimum_rating INTEGER DEFAULT 75")
    if "require_discord" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN require_discord INTEGER DEFAULT 1")
    if "require_rotrends" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN require_rotrends INTEGER DEFAULT 1")
    if "alert_level" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN alert_level TEXT DEFAULT 'all'")

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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS watched_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game_id INTEGER NOT NULL,
        game_name TEXT,
        last_players INTEGER,
        last_visits INTEGER,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, game_id)
    )
    """)

    # Migrate older watched_games tables that pre-date tracking columns.
    existing_watched_columns = set(_table_columns(cursor, "watched_games"))
    if "last_players" not in existing_watched_columns:
        cursor.execute("ALTER TABLE watched_games ADD COLUMN last_players INTEGER")
    if "last_visits" not in existing_watched_columns:
        cursor.execute("ALTER TABLE watched_games ADD COLUMN last_visits INTEGER")
    if "last_checked" not in existing_watched_columns:
        cursor.execute("ALTER TABLE watched_games ADD COLUMN last_checked TIMESTAMP")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ignored_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game_id INTEGER NOT NULL,
        date_ignored TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, game_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_analysis_cache (
        game_id INTEGER PRIMARY KEY,
        verdict TEXT,
        confidence INTEGER,
        strengths TEXT,
        risks TEXT,
        recommendation TEXT,
        raw TEXT,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gemini_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER,
        verdict TEXT,
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
    maximum_visits=None,
    minimum_players=None,
    maximum_players=None,
    minimum_growth=None,
    genre=None,
    max_age=None,
    alert_level=None,
):
    """Update user settings. Only updates fields that are provided."""
    from priority import normalize_setting  # local import: keeps top-level clean.

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    fields = []
    values = []

    if minimum_visits is not None:
        fields.append("minimum_visits = ?")
        values.append(int(minimum_visits))
    if maximum_visits is not None:
        fields.append("maximum_visits = ?")
        values.append(int(maximum_visits))
    if minimum_players is not None:
        fields.append("minimum_players = ?")
        values.append(int(minimum_players))
    if maximum_players is not None:
        fields.append("maximum_players = ?")
        values.append(int(maximum_players))
    if minimum_growth is not None:
        fields.append("minimum_growth = ?")
        values.append(int(minimum_growth))
    if genre is not None:
        fields.append("genre = ?")
        values.append(genre)
    if max_age is not None:
        fields.append("max_age = ?")
        values.append(int(max_age))
    if alert_level is not None:
        fields.append("alert_level = ?")
        values.append(normalize_setting(alert_level))

    if fields:
        query = f"UPDATE users SET {', '.join(fields)} WHERE discord_id = ?"
        values.append(discord_id)
        cursor.execute(query, values)
        conn.commit()

    conn.close()


def get_user_filters(discord_id):
    """Return a dict of filter settings for a user, normalized to the current defaults."""
    add_user(discord_id)

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT minimum_visits, maximum_visits,
           minimum_players, maximum_players,
           minimum_growth, minimum_rating, require_discord, require_rotrends,
           genre, max_age, alert_level
    FROM users
    WHERE discord_id = ?
    """, (discord_id,))

    row = cursor.fetchone()

    defaults = {
        "minimum_visits": 0,
        "maximum_visits": 1_500_000,
        "minimum_players": 15,
        "maximum_players": 2500,
        "minimum_growth": 0,
        "minimum_rating": 75,
        "require_discord": True,
        "require_rotrends": True,
        "genre": "Any",
        "max_age": 365,
        "alert_level": "all",
    }

    if row is None:
        return defaults

    normalized = dict(defaults)
    needs_update = False
    normalized.update(
        {
            "minimum_visits": int(row[0]) if row[0] is not None else defaults["minimum_visits"],
            "maximum_visits": int(row[1]) if row[1] is not None else defaults["maximum_visits"],
            "minimum_players": int(row[2]) if row[2] is not None else defaults["minimum_players"],
            "maximum_players": int(row[3]) if row[3] is not None else defaults["maximum_players"],
            "minimum_growth": int(row[4]) if row[4] is not None else defaults["minimum_growth"],
            "minimum_rating": int(row[5]) if row[5] is not None else defaults["minimum_rating"],
            "require_discord": bool(int(row[6])) if row[6] is not None else defaults["require_discord"],
            "require_rotrends": bool(int(row[7])) if row[7] is not None else defaults["require_rotrends"],
            "genre": row[8] or defaults["genre"],
            "max_age": int(row[9]) if row[9] is not None else defaults["max_age"],
            "alert_level": (row[10] or defaults["alert_level"]).lower(),
        }
    )

    # Migrate legacy/empty values to the new acquisition defaults when they are
    # still using the old broad defaults.
    if normalized["minimum_visits"] == 100000 and normalized["maximum_visits"] == 0:
        normalized["minimum_visits"] = defaults["minimum_visits"]
        normalized["maximum_visits"] = defaults["maximum_visits"]
        needs_update = True

    if normalized["minimum_players"] == 100 and normalized["maximum_players"] == 0:
        normalized["minimum_players"] = defaults["minimum_players"]
        normalized["maximum_players"] = defaults["maximum_players"]
        needs_update = True

    if normalized["minimum_rating"] in (0, None):
        normalized["minimum_rating"] = defaults["minimum_rating"]
        needs_update = True

    if normalized["require_discord"] is False:
        normalized["require_discord"] = defaults["require_discord"]
        needs_update = True

    if normalized["require_rotrends"] is False:
        normalized["require_rotrends"] = defaults["require_rotrends"]
        needs_update = True

    if needs_update:
        cursor.execute(
            """
            UPDATE users
            SET minimum_visits = ?, maximum_visits = ?, minimum_players = ?,
                maximum_players = ?, minimum_rating = ?, require_discord = ?,
                require_rotrends = ?
            WHERE discord_id = ?
            """,
            (
                normalized["minimum_visits"],
                normalized["maximum_visits"],
                normalized["minimum_players"],
                normalized["maximum_players"],
                normalized["minimum_rating"],
                int(normalized["require_discord"]),
                int(normalized["require_rotrends"]),
                discord_id,
            ),
        )
        conn.commit()

    conn.close()
    return normalized


def get_user_alert_level(discord_id):
    """Return the alert_level for a user, defaulting to 'all'."""
    row = get_user_row(discord_id)
    if row is None:
        return "all"
    # Index of alert_level depends on get_user_row's SELECT order — it does.
    try:
        return (row[8] or "all").lower()
    except IndexError:
        return "all"


def get_user_row(discord_id):
    """Return the raw users row including all Smart Filter columns."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT discord_id, minimum_visits, maximum_visits, "
        "minimum_players, maximum_players, minimum_growth, "
        "genre, max_age, alert_level FROM users WHERE discord_id = ?",
        (discord_id,),
    )

    row = cursor.fetchone()
    conn.close()
    return row


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


def watch_game_for_user(user_id, game):
    """Record a game on a user's active watch list (with snapshot stats)."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO watched_games
    (user_id, game_id, game_name, last_players, last_visits)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(user_id, game_id) DO UPDATE SET
        game_name = excluded.game_name,
        last_players = excluded.last_players,
        last_visits = excluded.last_visits,
        last_checked = CURRENT_TIMESTAMP
    """, (
        user_id,
        game["id"],
        game.get("name", ""),
        game.get("playing"),
        game.get("visits"),
    ))

    conn.commit()
    conn.close()


def get_watched_games_for_user(user_id):
    """Return all watched games for a user."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT game_id, game_name, last_players, last_visits, date_added
    FROM watched_games
    WHERE user_id = ?
    ORDER BY date_added DESC
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()
    return rows


def ignore_game_for_user(user_id, game_id):
    """Record a game the user wants suppressed from future alerts."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT OR IGNORE INTO ignored_games (user_id, game_id)
    VALUES (?, ?)
    """, (int(user_id), int(game_id)))

    conn.commit()
    conn.close()


def get_all_watched_games():
    """Return every watched game across all users for the tracker."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT user_id, game_id, game_name, last_players, last_visits, date_added
    FROM watched_games
    """)

    rows = cursor.fetchall()
    conn.close()
    return rows


def update_watched_game_snapshot(game_id, players, visits):
    """Update the stored CCU/visits snapshot for a tracked game."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    UPDATE watched_games
    SET last_players = ?,
        last_visits = ?,
        last_checked = CURRENT_TIMESTAMP
    WHERE game_id = ?
    """, (players, visits, game_id))

    conn.commit()
    conn.close()


def unwatch_game_for_user(user_id, game_id):
    """Remove a watched game for a specific user. Returns rows deleted."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    DELETE FROM watched_games
    WHERE user_id = ? AND game_id = ?
    """, (user_id, int(game_id)))

    removed = cursor.rowcount
    conn.commit()
    conn.close()
    return removed


def is_user_watching(user_id, game_id):
    """Return True if this user has this game in watched_games."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT 1 FROM watched_games
    WHERE user_id = ? AND game_id = ?
    """, (user_id, int(game_id)))

    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def get_ignored_games_for_user(user_id):
    """Return all games a user has ignored."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT game_id, date_ignored
    FROM ignored_games
    WHERE user_id = ?
    ORDER BY date_ignored DESC
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()
    return rows


def unignore_game_for_user(user_id, game_id):
    """Remove an ignored game for a specific user. Returns rows deleted."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    DELETE FROM ignored_games
    WHERE user_id = ? AND game_id = ?
    """, (user_id, int(game_id)))

    removed = cursor.rowcount
    conn.commit()
    conn.close()
    return removed
