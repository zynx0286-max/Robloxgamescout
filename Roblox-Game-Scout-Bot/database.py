import aiosqlite
from config import DATABASE_PATH


async def init_db():
    """Create tables if they don't exist yet."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT    NOT NULL,
                universe_id   INTEGER NOT NULL,
                name          TEXT    NOT NULL,
                playing       INTEGER DEFAULT 0,
                visits        INTEGER DEFAULT 0,
                saved_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, universe_id)
            )
            """
        )
        await db.commit()
    print("Database initialised.")


async def save_game(user_id: str, game: dict):
    """Insert or replace a game in the user's watchlist."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO watchlist (user_id, universe_id, name, playing, visits)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, universe_id) DO UPDATE SET
                name    = excluded.name,
                playing = excluded.playing,
                visits  = excluded.visits
            """,
            (
                user_id,
                game["universeId"],
                game["name"],
                game.get("playing", 0),
                game.get("visits", 0),
            ),
        )
        await db.commit()


async def get_saved_games(user_id: str) -> list[dict]:
    """Return all saved games for a user."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT universe_id, name, playing, visits FROM watchlist WHERE user_id = ? ORDER BY saved_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def remove_game(user_id: str, universe_id: int) -> bool:
    """Delete a game from the user's watchlist. Returns True if a row was deleted."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND universe_id = ?",
            (user_id, universe_id),
        )
        await db.commit()
    return cursor.rowcount > 0
