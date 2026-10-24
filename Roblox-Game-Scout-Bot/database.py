import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from config import DATABASE_PATH

logger = logging.getLogger("database")

DATABASE = DATABASE_PATH

# game_history grows by one row per scanned game per cycle. Without a cap
# it grows unboundedly (150 games x 96 cycles/day = ~14k rows/day). 30 days
# is ample for velocity (72h window) and growth math.
GAME_HISTORY_RETENTION_DAYS = 30
GAME_HISTORY_PRUNE_INTERVAL_HOURS = 6
_last_history_prune = None


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
        require_discord INTEGER DEFAULT 0,
        require_market_links INTEGER DEFAULT 1,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 0,
        alert_level TEXT DEFAULT 'all'
    )
    """)


# Wait up to this many seconds for a lock instead of failing with
# "database is locked" immediately. The bot writes snapshots every scan
# cycle while the portfolio worker/API can share the same file on a
# single-host deploy — short writes, but never a hard failure.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _connect():
    """Open the bot database with the shared connection contract.

    Pragmas live in sqlite_conn.tune() — one source of truth for every
    module that opens the shared file (busy_timeout 30 s,
    synchronous=NORMAL, temp_store=MEMORY; WAL itself is a file
    property, set once at create_database).
    """
    import sqlite_conn

    conn = sqlite3.connect(DATABASE, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    sqlite_conn.tune(conn)
    return conn


def create_database():
    conn = _connect()
    cursor = conn.cursor()

    # Only the original users-table columns force a rebuild. Every later
    # setting is added by the ALTER TABLE migrations below instead: the old
    # check rebuilt the table for ANY missing column, and _recreate_users_table
    # drops the table first — so adding a new setting used to wipe every
    # user's saved profile on the next restart.
    core_columns = {
        "discord_id",
        "minimum_visits",
        "minimum_players",
        "minimum_growth",
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
        require_discord INTEGER DEFAULT 0,
        require_market_links INTEGER DEFAULT 1,
        genre TEXT DEFAULT 'Any',
        max_age INTEGER DEFAULT 0,
        alert_level TEXT DEFAULT 'all',
        ping_subscribed INTEGER DEFAULT 0,
        require_charts INTEGER DEFAULT 0
    )
    """)

    existing_columns = set(_table_columns(cursor, "users"))
    if not core_columns.issubset(existing_columns):
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
        cursor.execute("ALTER TABLE users ADD COLUMN require_discord INTEGER DEFAULT 0")
    else:
        # require_discord defaulted to "Yes" back when Roblox's social-link
        # APIs were still open. Those endpoints now return HTTP 401, so an
        # invite is only visible in a game's description — leaving the old
        # default in place would mean requiring something the bot usually
        # cannot detect, and no game would ever alert. The stored default is
        # therefore reset to "No"; turning it back on is a one-command
        # decision with /settings.
        cursor.execute("UPDATE users SET require_discord = 0 WHERE require_discord = 1")
    if "require_market_links" not in existing_columns:
        # Migrate the old RoTrends requirement into require_market_links.
        if "require_rotrends" in existing_columns:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN require_market_links INTEGER DEFAULT 1"
            )
            cursor.execute(
                "UPDATE users SET require_market_links = require_rotrends"
            )
        else:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN require_market_links INTEGER DEFAULT 1"
            )
    if "alert_level" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN alert_level TEXT DEFAULT 'all'")
    if "ping_subscribed" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN ping_subscribed INTEGER DEFAULT 0")
    if "genre" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN genre TEXT DEFAULT 'Any'")
    if "max_age" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN max_age INTEGER DEFAULT 0")
    else:
        # max_age was stored but never read by any filter, so the untouched
        # default of 365 was never a decision anyone made. Now that it filters,
        # leaving those rows at 365 would silently drop most of the catalogue —
        # so the never-enforced default is reset to "no cap".
        cursor.execute("UPDATE users SET max_age = 0 WHERE max_age = 365")
    if "require_charts" not in existing_columns:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN require_charts INTEGER DEFAULT 0"
        )

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER,
        players INTEGER,
        visits INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Index for the hot path: velocity/growth read history per game ordered
    # by timestamp. Without this, every scan does full table scans that get
    # slower as history grows.
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_game_history_game_time
    ON game_history (game_id, timestamp)
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
        first_alerted TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        best_scout_score INTEGER,
        alert_count INTEGER DEFAULT 1
    )
    """)

    # Legacy alerted_games tables pre-date the leaderboard columns; add in
    # place like every other migration (existing rows get NULL score / 1
    # count, rendered as "—" and 1 by the leaderboard).
    existing_alerted_columns = set(_table_columns(cursor, "alerted_games"))
    if "best_scout_score" not in existing_alerted_columns:
        cursor.execute(
            "ALTER TABLE alerted_games ADD COLUMN best_scout_score INTEGER"
        )
    if "alert_count" not in existing_alerted_columns:
        cursor.execute(
            "ALTER TABLE alerted_games ADD COLUMN alert_count INTEGER DEFAULT 1"
        )

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
        source TEXT DEFAULT 'user',
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
    if "source" not in existing_watched_columns:
        # 'user' is the safe default: every watch that existed before the
        # auto-watch feature was, by definition, created by a human action.
        cursor.execute("ALTER TABLE watched_games ADD COLUMN source TEXT DEFAULT 'user'")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ignored_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game_id INTEGER NOT NULL,
        date_ignored TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, game_id)
    )
    """)

    # Hot-path indexes (created after every table exists, so legacy
    # databases pick them up on migration too):
    #  * watched_games (game_id) — tracker snapshot updates target one game
    #    across all its watch rows; the UNIQUE(user_id, game_id) index cannot
    #    serve a game_id-only lookup, so the tracker would full-scan per game
    #    per cycle.
    #  * scan_log (timestamp) — the morning digest counters and the retention
    #    prune filter by timestamp; scan_log is written several rows per
    #    cycle forever.
    #  * alerted_games (first_alerted) — the digest's "new since yesterday"
    #    window query.
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_watched_games_game
    ON watched_games (game_id)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_scan_log_timestamp
    ON scan_log (timestamp)
    """)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_alerted_games_first_alerted
    ON alerted_games (first_alerted)
    """)

    # Chart-rank history: one row per charted game per scan cycle, feeding
    # the rank-delta and best-rank signals in chart_rank.py. Written by
    # data_collector right after discovery; pruned on the same schedule as
    # game_history. cycle_ts is the writer's microsecond cycle stamp — the
    # column that lets "previous cycle" exclude "this cycle".
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chart_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        sort_id TEXT NOT NULL,
        sort_label TEXT,
        position INTEGER NOT NULL,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        cycle_ts TEXT
    )
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_chart_positions_game_time
    ON chart_positions (game_id, recorded_at)
    """)

    # Ascent Radar post-state: one row per game the radar has announced or
    # escalated. Written every radar tick that posts, so the per-game
    # cooldown clock keeps running even when the channel is misconfigured
    # (no burst of queued alerts the moment the channel comes back).
    # Additive like every table here — an existing database picks it up on
    # the next boot with no migration step.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ascent_state (
        game_id INTEGER PRIMARY KEY,
        game_name TEXT,
        last_kind TEXT,
        last_ccu INTEGER,
        first_seen_ts TEXT,
        last_alert_ts TEXT
    )
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_ascent_state_last_alert
    ON ascent_state (last_alert_ts)
    """)

    # Write-Ahead Logging: readers (portfolio API, health checks) never
    # block writers and vice versa. The mode is persistent per database
    # file, so setting it once at startup is enough for every connection.
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass  # e.g. read-only filesystem — rollback journal still works

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

    # Portfolio live-data tables (kept separate from the bot tables above).
    from portfolio_db import create_portfolio_tables
    create_portfolio_tables()


def add_user(discord_id):
    """Create the user row if it is missing. Never touches an existing row.

    The modern defaults are written explicitly rather than inherited from the
    column definitions: SQLite bakes a DEFAULT into the table when it is
    created and never updates it, so a database made before this version would
    hand every new user the old values (require_discord=1, max_age=365) even
    though the code's defaults have moved on. `INSERT OR IGNORE` means an
    existing profile — and any choice the user made in /settings — is left
    exactly as it is.
    """
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO users
            (discord_id, require_discord, max_age, require_charts)
        VALUES (?, 0, 0, 0)
        """,
        (discord_id,),
    )

    conn.commit()
    conn.close()


def get_user(discord_id):
    conn = _connect()
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
    minimum_rating=None,
    require_discord=None,
    require_charts=None,
    ping_subscribed=None,
):
    """Update user settings. Only updates fields that are provided.

    Self-initializing: ensures the user row exists first, so an UPDATE can
    never silently no-op against a missing row.
    """
    from priority import normalize_setting  # local import: keeps top-level clean.

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO users (discord_id) VALUES (?)", (discord_id,)
    )

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
    if minimum_rating is not None:
        fields.append("minimum_rating = ?")
        values.append(int(minimum_rating))
    if require_discord is not None:
        fields.append("require_discord = ?")
        values.append(int(bool(require_discord)))
    if require_charts is not None:
        fields.append("require_charts = ?")
        values.append(int(bool(require_charts)))
    if ping_subscribed is not None:
        fields.append("ping_subscribed = ?")
        values.append(int(bool(ping_subscribed)))

    if fields:
        query = f"UPDATE users SET {', '.join(fields)} WHERE discord_id = ?"
        values.append(discord_id)
        cursor.execute(query, values)
        conn.commit()

    conn.close()


def get_user_filters(discord_id):
    """Return a dict of filter settings for a user, normalized to the current defaults."""
    add_user(discord_id)

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT minimum_visits, maximum_visits,
           minimum_players, maximum_players,
           minimum_growth, minimum_rating, require_discord, require_market_links,
           genre, max_age, alert_level, ping_subscribed, require_charts
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
        "require_discord": False,
        "require_charts": False,
        "genre": "Any",
        "max_age": 0,
        "alert_level": "all",
        "ping_subscribed": False,
    }

    if row is None:
        return defaults

    normalized = dict(defaults)
    normalized.update(
        {
            "minimum_visits": int(row[0]) if row[0] is not None else defaults["minimum_visits"],
            "maximum_visits": int(row[1]) if row[1] is not None else defaults["maximum_visits"],
            "minimum_players": int(row[2]) if row[2] is not None else defaults["minimum_players"],
            "maximum_players": int(row[3]) if row[3] is not None else defaults["maximum_players"],
            "minimum_growth": int(row[4]) if row[4] is not None else defaults["minimum_growth"],
            "minimum_rating": int(row[5]) if row[5] is not None else defaults["minimum_rating"],
            "require_discord": bool(int(row[6])) if row[6] is not None else defaults["require_discord"],
            "genre": row[8] or defaults["genre"],
            "max_age": int(row[9]) if row[9] is not None else defaults["max_age"],
            "alert_level": (row[10] or defaults["alert_level"]).lower(),
            "ping_subscribed": bool(int(row[11])) if row[11] is not None else defaults["ping_subscribed"],
            "require_charts": bool(int(row[12])) if row[12] is not None else defaults["require_charts"],
        }
    )

    # Migrate legacy/empty values to the new acquisition defaults when they are
    # still using the old broad defaults.
    if normalized["minimum_visits"] == 100000 and normalized["maximum_visits"] == 0:
        normalized["minimum_visits"] = defaults["minimum_visits"]
        normalized["maximum_visits"] = defaults["maximum_visits"]

    if normalized["minimum_players"] == 100 and normalized["maximum_players"] == 0:
        normalized["minimum_players"] = defaults["minimum_players"]
        normalized["maximum_players"] = defaults["maximum_players"]

    # Legacy/empty values are normalized in memory above and below — the
    # read path never writes user settings back to the database.

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
    conn = _connect()
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
    save_game_snapshots_bulk([game])


def save_game_snapshots_bulk(games):
    """Save many game snapshots in ONE transaction.

    A scan cycle writes one row per collected game; doing that with one
    connection + commit per game (~150 per cycle) costs ~150 WAL fsyncs and
    150 connection setups/teardowns. One transaction writes the same rows
    for the cost of a single fsync. Pruning runs exactly as it does for the
    single-row path. Handles an empty list as a cheap no-op so callers
    never need a special case.
    """
    global _last_history_prune
    rows = []
    for game in games or []:
        try:
            rows.append((game["id"], game["playing"], game["visits"]))
        except (KeyError, TypeError):
            continue
    if not rows:
        return 0

    conn = _connect()
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO game_history
        (game_id, players, visits)
        VALUES (?, ?, ?)
        """,
        rows,
    )
    written = cursor.rowcount
    conn.commit()
    conn.close()

    # Opportunistic pruning: at most every PRUNE_INTERVAL_HOURS, drop rows
    # older than GAME_HISTORY_RETENTION_DAYS so the table can't grow forever.
    now = datetime.now(timezone.utc)
    if (
        _last_history_prune is None
        or now - _last_history_prune
        >= timedelta(hours=GAME_HISTORY_PRUNE_INTERVAL_HOURS)
    ):
        try:
            cutoff = (
                now.replace(tzinfo=None) - timedelta(days=GAME_HISTORY_RETENTION_DAYS)
            ).isoformat(sep=" ")
            conn = _connect()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM game_history WHERE timestamp < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            _last_history_prune = now
            if deleted:
                logger.info("Pruned %d game_history rows older than %dd", deleted, GAME_HISTORY_RETENTION_DAYS)
        except sqlite3.Error as exc:
            logger.warning("game_history pruning failed: %s", exc)

    return written
def save_game_for_user(user_id, game):
    """Save a game to a user's watchlist."""
    conn = _connect()
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
    conn = _connect()
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
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM alerted_games WHERE game_id = ?",
        (game_id,),
    )
    exists = cursor.fetchone() is not None

    conn.close()
    return exists


SCAN_LOG_RETENTION_DAYS = 30
SCAN_LOG_PRUNE_INTERVAL_HOURS = 6
_last_scan_log_prune = None


def save_alert_log(event, message):
    """Append a single row to the scan_log table.

    scan_log is written several rows per scan cycle for the life of the
    bot; without retention it is the one table that grows unbounded on a
    24/7 host. Old rows are pruned opportunistically (at most every few
    hours), matching the game_history and chart_positions schedule.
    """
    global _last_scan_log_prune

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO scan_log (event, message)
    VALUES (?, ?)
    """, (event, message))

    now = datetime.now(timezone.utc)
    due = (
        _last_scan_log_prune is None
        or (now - _last_scan_log_prune)
        >= timedelta(hours=SCAN_LOG_PRUNE_INTERVAL_HOURS)
    )
    if due:
        cutoff = (
            now - timedelta(days=SCAN_LOG_RETENTION_DAYS)
        ).isoformat(sep=" ")
        try:
            cursor.execute(
                "DELETE FROM scan_log WHERE timestamp < ?", (cutoff,)
            )
            _last_scan_log_prune = now
        except sqlite3.Error:
            pass  # retention is best-effort; the INSERT above is committed

    conn.commit()
    conn.close()


def watch_game_for_user(user_id, game):
    """Record a game on a user's active watch list (with snapshot stats).

    The row is stamped source='user' on insert AND on update: an explicit
    human watch must never be misread as a prunable system watch — that
    matters when AUTO_WATCH_USER_ID points at a real member instead of 0.
    """
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO watched_games
    (user_id, game_id, game_name, last_players, last_visits, source)
    VALUES (?, ?, ?, ?, ?, 'user')
    ON CONFLICT(user_id, game_id) DO UPDATE SET
        game_name = excluded.game_name,
        last_players = excluded.last_players,
        last_visits = excluded.last_visits,
        last_checked = CURRENT_TIMESTAMP,
        source = 'user'
    """, (
        user_id,
        game["id"],
        game.get("name", ""),
        game.get("playing"),
        game.get("visits"),
    ))

    conn.commit()
    conn.close()


def auto_watch_game(game, user_id=None, source="auto"):
    """Record a system watch for a game the bot just alerted on.

    Creates a watched_games row owned by *user_id* (default: the configured
    AUTO_WATCH_USER_ID, normally 0 = the bot itself) so the tracker keeps an
    eye on the game after its one-time alert. Unlike watch_game_for_user,
    an existing row is left exactly as it is — a watch the user created
    must never be rewritten by the system, and re-alerting the same game is
    already prevented by the alerted_games table.

    Returns True when a new row was inserted, False when one already existed.
    The AUTO_WATCH_MAX prune runs only on actual inserts.
    """
    from config import AUTO_WATCH_USER_ID  # local import: keeps top-level clean.

    owner = int(user_id) if user_id is not None else AUTO_WATCH_USER_ID
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO watched_games
    (user_id, game_id, game_name, last_players, last_visits, source)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id, game_id) DO NOTHING
    """, (
        owner,
        game["id"],
        game.get("name", ""),
        game.get("playing"),
        game.get("visits"),
        source,
    ))

    inserted = cursor.rowcount == 1
    conn.commit()
    conn.close()

    if inserted:
        prune_auto_watches()
    return inserted


def prune_auto_watches(cap=None):
    """Trim system watches (source='auto') down to the AUTO_WATCH_MAX cap.

    Only rows whose source is 'auto' are ever deleted — a user's own watch
    is never pruned by this. When the cap is exceeded, the OLDEST system
    watches go first: the newest alerts are the ones still worth tracking.
    A cap of 0 (or less) removes every system watch, which is how the
    AUTO_WATCH_MAX=0 'no automatic tracking' configuration works.
    """
    from config import AUTO_WATCH_MAX  # local import: keeps top-level clean.

    cap = int(cap) if cap is not None else AUTO_WATCH_MAX

    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM watched_games WHERE source = 'auto'")
    count = int(cursor.fetchone()[0])
    if count <= cap:
        conn.close()
        return 0

    excess = count - cap
    cursor.execute("""
    DELETE FROM watched_games
    WHERE id IN (
        SELECT id FROM watched_games
        WHERE source = 'auto'
        ORDER BY date_added ASC, id ASC
        LIMIT ?
    )
    """, (excess,))

    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(
            "Pruned %d system watch(es) at the AUTO_WATCH_MAX=%d cap",
            deleted,
            cap,
        )
    return deleted


def count_auto_watches():
    """Number of system watches currently tracked."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM watched_games WHERE source = 'auto'")
    count = int(cursor.fetchone()[0])
    conn.close()
    return count


def get_watched_games_for_user(user_id):
    """Return all watched games for a user."""
    conn = _connect()
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
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT OR IGNORE INTO ignored_games (user_id, game_id)
    VALUES (?, ?)
    """, (int(user_id), int(game_id)))

    conn.commit()
    conn.close()


def get_all_watched_games():
    """Return every watched game across all users for the tracker.

    Rows are (user_id, game_id, game_name, last_players, last_visits,
    date_added, source). The source column lets the tracker tell a system
    (auto) watch from a human one without hard-coding the owner's ID.
    """
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT user_id, game_id, game_name, last_players, last_visits, date_added, source
    FROM watched_games
    """)

    rows = cursor.fetchall()
    conn.close()
    return rows


def update_watched_game_snapshot(game_id, players, visits):
    """Update the stored CCU/visits snapshot for a tracked game."""
    conn = _connect()
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


def update_watched_game_snapshots_bulk(updates):
    """Update snapshots for a whole tracker cycle in ONE transaction.

    `updates` is an iterable of (game_id, players, visits). The tracker
    used to open a connection + commit per watched game — one fsync
    storm per cycle against the same table the scan also writes. Same
    rows, same CURRENT_TIMESTAMP stamping, one commit instead of N.
    No-op on an empty batch so callers need no special case.
    """
    rows = []
    for row in updates:
        if len(row) != 3:
            raise ValueError(
                "update_watched_game_snapshots_bulk expects (game_id, players, visits)"
            )
        rows.append((row[1], row[2], row[0]))
    if not rows:
        return
    conn = _connect()
    try:
        conn.cursor().executemany(
            """
            UPDATE watched_games
            SET last_players = ?,
                last_visits = ?,
                last_checked = CURRENT_TIMESTAMP
            WHERE game_id = ?
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def unwatch_game_for_user(user_id, game_id):
    """Remove a watched game for a specific user. Returns rows deleted."""
    conn = _connect()
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
    conn = _connect()
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
    conn = _connect()
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
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
    DELETE FROM ignored_games
    WHERE user_id = ? AND game_id = ?
    """, (user_id, int(game_id)))

    removed = cursor.rowcount
    conn.commit()
    conn.close()
    return removed


def get_pinged_subscribers():
    """Return discord_ids of users subscribed to discovery-scan pings."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT discord_id FROM users WHERE ping_subscribed = 1"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def is_user_ignoring(user_id, game_id):
    """True if the user has rejected/ignored this game (❌ Reject button)."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM ignored_games WHERE user_id = ? AND game_id = ? LIMIT 1",
        (int(user_id), int(game_id)),
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None


def mark_alerted(game_id, game_name, scout_score=None):
    """Record (or re-record) an alert, keeping the best Scout Score seen.

    INSERT OR IGNORE preserves the first_alerted timestamp; on re-alert the
    name and the higher of the two Scout Scores are kept and alert_count is
    incremented — so the leaderboard can show both peak quality and repeat
    discovery frequency. Existing callers that pass no score keep working
    (score stays whatever it was).
    """
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO alerted_games (game_id, game_name, best_scout_score, alert_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(game_id) DO UPDATE SET
            game_name = excluded.game_name,
            best_scout_score = MAX(
                COALESCE(alerted_games.best_scout_score, excluded.best_scout_score),
                COALESCE(excluded.best_scout_score, -1)
            ),
            alert_count = alerted_games.alert_count + 1
        """,
        (int(game_id), str(game_name or ""),
         int(scout_score) if scout_score is not None else None),
    )
    conn.commit()
    conn.close()


def top_alerted_by_scout_score(limit: int = 10, days: int = 30):
    """The leaderboard: highest Scout Scores the bot has actually alerted.

    Returns rows of (game_id, game_name, best_scout_score, alert_count,
    first_alerted) sorted by best score, then alert frequency (repeat
    discoveries are themselves a signal), then recency. Games with no
    recorded score sort last. days=0 means all-time.
    """
    conn = _connect()
    cursor = conn.cursor()
    cutoff_clause = ""
    params: list = []
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(sep=" ")
        cutoff_clause = "WHERE first_alerted >= ?"
        params.append(cutoff)
    cursor.execute(
        f"""
        SELECT game_id, game_name, best_scout_score, alert_count, first_alerted
        FROM alerted_games
        {cutoff_clause}
        ORDER BY
            COALESCE(best_scout_score, -1) DESC,
            alert_count DESC,
            first_alerted DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def alert_heatmap(days: int = 28):
    """Alert volume by ISO week for the /heatmap grid.

    Returns rows of (iso_week_start_iso, count, avg_best_scout_score) for
    weeks with at least one alert inside the window, oldest first. Scores
    recorded NULL (legacy rows) count toward volume from the average.
    days=0 means all time.
    """
    conn = _connect()
    cursor = conn.cursor()
    params: list = []
    where = ""
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(sep=" ")
        where = "WHERE first_alerted >= ?"
        params.append(cutoff)
    cursor.execute(
        f"""
        SELECT strftime('%Y-W%W', first_alerted) AS week,
               COUNT(*) AS alerts,
               AVG(best_scout_score) AS avg_score
        FROM alerted_games
        {where}
        GROUP BY week
        ORDER BY week ASC
        """,
        params,
    )
    rows = cursor.fetchall()
    conn.close()
    return [(r[0], int(r[1]), (float(r[2]) if r[2] is not None else None))
            for r in rows]


def get_alerted_games_since(hours: int = 24, limit: int = 10):
    """Return recently alerted games for the morning digest.

    Returns rows of (game_name, first_alerted) for games whose first alert
    was within the last *hours* hours, newest first, capped at *limit*.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        sep=" "
    )
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT game_name, first_alerted
        FROM alerted_games
        WHERE first_alerted >= ?
        ORDER BY first_alerted DESC
        LIMIT ?
        """,
        (cutoff, int(limit)),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def count_alerted_since(hours: int = 24) -> int:
    """Count games first alerted within the last *hours* hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        sep=" "
    )
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM alerted_games WHERE first_alerted >= ?",
        (cutoff,),
    )
    n = cursor.fetchone()[0]
    conn.close()
    return int(n)


def count_scan_cycles_since(hours: int = 24) -> int:
    """Count completed scan cycles logged in scan_log within *hours* hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        sep=" "
    )
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM scan_log WHERE event = 'scan_complete' "
        "AND timestamp >= ?",
        (cutoff,),
    )
    n = cursor.fetchone()[0]
    conn.close()
    return int(n)
