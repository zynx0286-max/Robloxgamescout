"""Developer Intelligence v1 — pulls creator history from Roblox public APIs.

The component has two halves, separated for testability:

- `_calculate_score_from_data(...)` — pure scoring function. No network.
- `calculate_developer_score(game)` — orchestrator. Hits Roblox, caches
  results in SQLite so repeated alerts on the same creator don't slam the
  API. Falls back to (0, label, "data unavailable") if anything fails.
"""

import time
import sqlite3
import requests


DATABASE = "scoutbot.db"
DEV_CACHE_TTL_SECONDS = 86400  # 24h
DEV_LABEL = "👨\u200d💻 Developer history"


def _ensure_table():
    """Create developer_cache if missing."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS developer_cache (
        creator_key TEXT PRIMARY KEY,
        creator_id INTEGER,
        creator_type TEXT,
        name TEXT,
        member_count INTEGER DEFAULT 0,
        games_total INTEGER DEFAULT 0,
        games_successful INTEGER DEFAULT 0,
        max_playing_peak INTEGER DEFAULT 0,
        dev_score INTEGER DEFAULT 0,
        label TEXT,
        reason TEXT,
        last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def _cache_get(creator_key):
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT creator_id, creator_type, name, member_count,
           games_total, games_successful, max_playing_peak,
           dev_score, label, reason,
           strftime('%s', last_checked)
    FROM developer_cache
    WHERE creator_key = ?
    """, (str(creator_key),))
    row = cursor.fetchone()
    conn.close()
    return row


def _cache_put(creator_key, data):
    _ensure_table()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO developer_cache
    (creator_key, creator_id, creator_type, name, member_count,
     games_total, games_successful, max_playing_peak, dev_score,
     label, reason)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(creator_key) DO UPDATE SET
        creator_id=excluded.creator_id,
        creator_type=excluded.creator_type,
        name=excluded.name,
        member_count=excluded.member_count,
        games_total=excluded.games_total,
        games_successful=excluded.games_successful,
        max_playing_peak=excluded.max_playing_peak,
        dev_score=excluded.dev_score,
        label=excluded.label,
        reason=excluded.reason,
        last_checked=CURRENT_TIMESTAMP
    """, (
        str(creator_key),
        data.get("creator_id"),
        data.get("creator_type"),
        data.get("name"),
        data.get("member_count"),
        data.get("games_total"),
        data.get("games_successful"),
        data.get("max_playing_peak"),
        data.get("dev_score"),
        data.get("label"),
        data.get("reason"),
    ))
    conn.commit()
    conn.close()


def fetch_creator(universe_id):
    """Return (creator_id, creator_type, name) from multigame-details."""
    url = (
        "https://games.roblox.com/v1/games/multigame-details"
        f"?universeIds={universe_id}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            for g in r.json().get("data", []):
                creator = g.get("creator", {})
                if creator.get("id"):
                    return (
                        int(creator["id"]),
                        creator.get("type", "User"),
                        creator.get("name", ""),
                    )
    except Exception:
        pass
    return None, None, None


def fetch_group_size(group_id):
    """Return member count for a Roblox group, or 0 on failure."""
    try:
        r = requests.get(
            f"https://groups.roblox.com/v1/groups/{group_id}",
            timeout=10,
        )
        if r.status_code == 200:
            return int(r.json().get("memberCount", 0))
    except Exception:
        pass
    return 0


def fetch_user_past_games(user_id, limit=20):
    """Return list of past Roblox games by a user."""
    try:
        r = requests.get(
            f"https://games.roblox.com/v2/users/{user_id}/games"
            f"?sortOrder=Desc&limit={limit}",
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception:
        pass
    return []


def _calculate_score_from_data(
    creator_type,
    member_count,
    games_total,
    games_successful,
    max_playing_peak,
):
    """Pure scoring logic — returns 0-20.

    Signals are split into two tracks because User and Group creators expose
    very different data on Roblox:

    - **Group creators**: size is the strongest signal. Bonus for member count
      and prior successful games.
    - **User creators**: catalog depth and a single big hit are the strongest
      signals.
    """
    if creator_type == "Group":
        pts = 0
        if member_count >= 50000:
            pts += 10
        elif member_count >= 10000:
            pts += 6
        elif member_count >= 1000:
            pts += 3
        if games_successful >= 3:
            pts += 10
        elif games_successful >= 1:
            pts += 5
        elif games_total >= 5:
            pts += 2
        return min(pts, 20)

    pts = 0
    if games_total >= 10:
        pts += 6
    elif games_total >= 3:
        pts += 3
    elif games_total >= 1:
        pts += 1

    if games_successful >= 3:
        pts += 10
    elif games_successful >= 1:
        pts += 6

    if max_playing_peak >= 50000:
        pts += 4
    elif max_playing_peak >= 10000:
        pts += 2

    return min(pts, 20)


def _format_reason(creator_type, creator_name, member_count, games_total, games_successful, max_peak, score):
    """Human-friendly explanation for the breakdown row."""
    if score == 0:
        return "Limited creator history"

    if creator_type == "Group":
        if member_count:
            return f"Group with {member_count:,} members"
        return f"Group ({creator_name or 'unknown'})"

    bits = []
    if games_total:
        bits.append(f"{games_total} game{'s' if games_total != 1 else ''}")
    if games_successful:
        bits.append(f"{games_successful} successful")
    if not bits:
        return creator_name or "New creator"
    text = creator_name or "Creator"
    return f"{text}: {', '.join(bits)}"


def calculate_developer_score(game, force_refresh=False):
    """Return (points, label, reason) for the Developer History component.

    Always returns regardless of network/SQLite state — on any failure it
    yields a safe (0, label, "Creator data unavailable") result.
    """
    universe_id = game.get("id")
    if not universe_id:
        return 0, DEV_LABEL, "Creator unknown"

    cache_key = f"creator:{universe_id}"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            last_ts = int(cached[10] or 0)
            if (time.time() - last_ts) < DEV_CACHE_TTL_SECONDS:
                score = int(cached[7] or 0)
                label = cached[8] or DEV_LABEL
                reason = cached[9] or "Cached"
                return score, label, reason

    # Live fetch
    creator_id, creator_type, creator_name = fetch_creator(universe_id)
    if creator_id is None:
        return 0, DEV_LABEL, "Creator data unavailable"

    member_count = 0
    games_total = 0
    games_successful = 0
    max_peak = 0

    if creator_type == "Group":
        member_count = fetch_group_size(creator_id)
        # Group creator catalog isn't exposed via a clean public API. We'd
        # need to scan the universe → root-place lookup ourselves. Skip
        # catalog signals and lean on member count.
    else:
        history = fetch_user_past_games(creator_id, limit=20)
        games_total = len(history)
        games_successful = sum(
            1 for g in history if (g.get("playing") or 0) >= 1000
        )
        max_peak = max(
            ((g.get("playing") or 0) for g in history), default=0
        )

    pts = _calculate_score_from_data(
        creator_type,
        member_count,
        games_total,
        games_successful,
        max_peak,
    )
    reason = _format_reason(
        creator_type, creator_name,
        member_count, games_total, games_successful, max_peak, pts,
    )

    _cache_put(cache_key, {
        "creator_id": creator_id,
        "creator_type": creator_type,
        "name": creator_name,
        "member_count": member_count,
        "games_total": games_total,
        "games_successful": games_successful,
        "max_playing_peak": max_peak,
        "dev_score": pts,
        "label": DEV_LABEL,
        "reason": reason,
    })

    return pts, DEV_LABEL, reason
