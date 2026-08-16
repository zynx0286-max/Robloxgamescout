"""Portfolio live-data storage.

Dedicated tables backing the public portfolio API. These are deliberately
separate from the bot's scan/watch/alert tables: the portfolio only ever
exposes what you explicitly put on it, and raw Roblox numbers are kept
apart from your professional info (role, description, links).

Tables
------
portfolio_games   -- one row per showcased game: game_id + your info.
game_snapshots    -- time series of raw Roblox stats per game.
discord_snapshots -- optional Discord member counts (only where authorized).
"""

import sqlite3
from datetime import datetime, timedelta
from config import DATABASE_PATH

DATABASE = DATABASE_PATH


def create_portfolio_tables():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT DEFAULT '',
        description TEXT DEFAULT '',
        project_url TEXT DEFAULT '',
        roblox_url TEXT DEFAULT '',
        discord_url TEXT DEFAULT '',
        thumbnail_url TEXT DEFAULT '',
        visible INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        players INTEGER,
        visits INTEGER,
        favorites INTEGER,
        likes INTEGER,
        dislikes INTEGER,
        like_ratio REAL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS discord_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        member_count INTEGER
    )
    """)

    # Keep the snapshot tables from growing forever: retain ~90 days for
    # the 7d/30d charts the API serves.
    cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat(sep=" ")
    cursor.execute("DELETE FROM game_snapshots WHERE timestamp < ?", (cutoff,))
    cursor.execute("DELETE FROM discord_snapshots WHERE timestamp < ?", (cutoff,))

    conn.commit()
    conn.close()


def _connect():
    return sqlite3.connect(DATABASE)


def add_portfolio_game(
    game_id,
    display_name,
    role="",
    description="",
    project_url="",
    roblox_url="",
    discord_url="",
    thumbnail_url="",
    visible=True,
    sort_order=0,
):
    """Register a game on the portfolio. Upsert on game_id."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO portfolio_games
        (game_id, display_name, role, description, project_url,
         roblox_url, discord_url, thumbnail_url, visible, sort_order)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(game_id) DO UPDATE SET
        display_name = excluded.display_name,
        role = excluded.role,
        description = excluded.description,
        project_url = excluded.project_url,
        roblox_url = excluded.roblox_url,
        discord_url = excluded.discord_url,
        thumbnail_url = excluded.thumbnail_url,
        visible = excluded.visible,
        sort_order = excluded.sort_order
    """, (
        int(game_id),
        display_name,
        role,
        description,
        project_url,
        roblox_url,
        discord_url,
        thumbnail_url,
        int(bool(visible)),
        int(sort_order),
    ))
    conn.commit()
    conn.close()


def remove_portfolio_game(game_id):
    """Unregister a game (and drop its snapshots) from the portfolio."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM portfolio_games WHERE game_id = ?", (int(game_id),))
    cursor.execute("DELETE FROM game_snapshots WHERE game_id = ?", (int(game_id),))
    cursor.execute("DELETE FROM discord_snapshots WHERE game_id = ?", (int(game_id),))
    conn.commit()
    conn.close()


def list_portfolio_games(visible_only=True):
    conn = _connect()
    cursor = conn.cursor()
    query = """
        SELECT game_id, display_name, role, description, project_url,
               roblox_url, discord_url, thumbnail_url, visible, sort_order
        FROM portfolio_games
    """
    params = ()
    if visible_only:
        query += " WHERE visible = 1"
    query += " ORDER BY sort_order ASC, display_name ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "game_id": r[0],
            "display_name": r[1],
            "role": r[2],
            "description": r[3],
            "project_url": r[4],
            "roblox_url": r[5],
            "discord_url": r[6],
            "thumbnail_url": r[7],
            "visible": bool(r[8]),
            "sort_order": r[9],
        }
        for r in rows
    ]


def get_portfolio_game(game_id):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT game_id, display_name, role, description, project_url,
               roblox_url, discord_url, thumbnail_url, visible, sort_order
        FROM portfolio_games WHERE game_id = ?
    """, (int(game_id),))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "game_id": row[0],
        "display_name": row[1],
        "role": row[2],
        "description": row[3],
        "project_url": row[4],
        "roblox_url": row[5],
        "discord_url": row[6],
        "thumbnail_url": row[7],
        "visible": bool(row[8]),
        "sort_order": row[9],
    }


def update_portfolio_game(game_id, **fields):
    """Update editable portfolio fields. Returns True if a row was touched."""
    allowed = {
        "display_name", "role", "description", "project_url",
        "roblox_url", "discord_url", "thumbnail_url", "visible", "sort_order",
    }
    sets = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "visible":
            value = int(bool(value))
        if key == "sort_order":
            value = int(value)
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return False
    values.append(int(game_id))
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE portfolio_games SET {', '.join(sets)} WHERE game_id = ?",
        values,
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def record_game_snapshot(game_id, players, visits, favorites=None,
                         likes=None, dislikes=None, like_ratio=None):
    if like_ratio is None and likes is not None and dislikes is not None:
        total_votes = likes + dislikes
        if total_votes > 0:
            like_ratio = round(likes / total_votes * 100.0, 2)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO game_snapshots
        (game_id, players, visits, favorites, likes, dislikes, like_ratio)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (int(game_id), players, visits, favorites, likes, dislikes, like_ratio))
    conn.commit()
    conn.close()


def get_latest_snapshot(game_id):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, players, visits, favorites, likes, dislikes, like_ratio
        FROM game_snapshots WHERE game_id = ?
        ORDER BY timestamp DESC, id DESC LIMIT 1
    """, (int(game_id),))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "timestamp": row[0],
        "players": row[1],
        "visits": row[2],
        "favorites": row[3],
        "likes": row[4],
        "dislikes": row[5],
        "like_ratio": row[6],
    }


def get_snapshots(game_id, hours=None, limit=None):
    """Return snapshot rows for a game, newest first. `hours` filters the
    window (None = all retained), `limit` caps the row count."""
    conn = _connect()
    cursor = conn.cursor()
    query = (
        "SELECT timestamp, players, visits, favorites, likes, dislikes, like_ratio "
        "FROM game_snapshots WHERE game_id = ?"
    )
    params = [int(game_id)]
    if hours is not None:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat(sep=" ")
        query += " AND timestamp >= ?"
        params.append(cutoff)
    query += " ORDER BY timestamp ASC, id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "timestamp": r[0],
            "players": r[1],
            "visits": r[2],
            "favorites": r[3],
            "likes": r[4],
            "dislikes": r[5],
            "like_ratio": r[6],
        }
        for r in rows
    ]


def compute_growth(game_id, hours):
    """Return pct change of players/visits between a snapshot `hours` ago
    (or the oldest available) and the latest one. Values are None when
    there is no comparable data."""
    rows = get_snapshots(game_id, hours=hours)
    if len(rows) < 2:
        return {"players": None, "visits": None}
    oldest = rows[0]
    latest = rows[-1]
    return {
        "players": _pct_change(oldest["players"], latest["players"]),
        "visits": _pct_change(oldest["visits"], latest["visits"]),
    }


def _pct_change(before, after):
    if before is None or after is None or before == 0:
        return None
    return round((after - before) / before * 100.0, 2)


def get_peak_players(game_id, hours=None):
    """Highest players value seen in the retained window."""
    rows = get_snapshots(game_id, hours=hours)
    values = [r["players"] for r in rows if r["players"] is not None]
    return max(values) if values else None


def record_discord_snapshot(game_id, member_count):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO discord_snapshots (game_id, member_count)
    VALUES (?, ?)
    """, (int(game_id), int(member_count)))
    conn.commit()
    conn.close()


def get_latest_discord_snapshot(game_id):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, member_count FROM discord_snapshots
        WHERE game_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1
    """, (int(game_id),))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {"timestamp": row[0], "member_count": row[1]}
