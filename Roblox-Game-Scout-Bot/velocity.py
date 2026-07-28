"""
Velocity-based trending detection.

Computes game momentum from historical snapshots stored in game_history.
Detects:
  - CCU spikes (current >> rolling average)
  - Visit acceleration (second derivative of visit count)
  - Sustained growth (consistent upward trend over N snapshots)
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import DATABASE_PATH, MIN_VELOCITY_SNAPSHOTS, VELOCITY_SPIKE_THRESHOLD

logger = logging.getLogger("velocity")

# How far back to look for historical snapshots (in hours)
_VELOCITY_WINDOW_HOURS = 72

# Minimum snapshots needed for a reliable velocity calculation
_MIN_SNAPSHOTS = MIN_VELOCITY_SNAPSHOTS


def get_historical_snapshots(
    game_id: int,
    hours: int = _VELOCITY_WINDOW_HOURS,
    limit: int = 50,
) -> list[dict]:
    """
    Fetch historical snapshots for a game from the database.

    Returns list of dicts with 'players', 'visits', 'timestamp' keys,
    ordered oldest first.
    """
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute("""
    SELECT players, visits, timestamp
    FROM game_history
    WHERE game_id = ?
      AND timestamp >= datetime('now', ?)
    ORDER BY timestamp ASC
    LIMIT ?
    """, (game_id, f'-{hours} hours', limit))

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "players": row[0],
            "visits": row[1],
            "timestamp": row[2],
        }
        for row in rows
    ]


def compute_velocity(game_id: int) -> dict:
    """
    Compute velocity metrics for a game.

    Returns a dict with:
      - ccu_spike: float (ratio of current to rolling avg, 1.0 = average)
      - ccu_trend: str ("spiking", "growing", "stable", "declining")
      - visit_acceleration: float (second derivative of visits)
      - growth_consistency: float (0-1, how consistently the game is growing)
      - snapshot_count: int (number of snapshots used)
      - avg_players: float (rolling average CCU)
      - current_players: int (most recent CCU)
    """
    snapshots = get_historical_snapshots(game_id)

    result = {
        "ccu_spike": 1.0,
        "ccu_trend": "insufficient_data",
        "visit_acceleration": 0.0,
        "growth_consistency": 0.0,
        "snapshot_count": len(snapshots),
        "avg_players": 0.0,
        "current_players": 0,
    }

    if len(snapshots) < _MIN_SNAPSHOTS:
        return result

    # Extract player counts
    player_counts = [s["players"] for s in snapshots]
    visit_counts = [s["visits"] for s in snapshots]

    current_players = player_counts[-1]
    avg_players = sum(player_counts) / len(player_counts)

    result["current_players"] = current_players
    result["avg_players"] = round(avg_players, 1)

    # CCU spike: ratio of current to rolling average
    if avg_players > 0:
        spike_ratio = current_players / avg_players
        result["ccu_spike"] = round(spike_ratio, 2)
    else:
        spike_ratio = 1.0

    # CCU trend classification
    if spike_ratio >= VELOCITY_SPIKE_THRESHOLD:
        result["ccu_trend"] = "spiking"
    elif spike_ratio >= 1.2:
        result["ccu_trend"] = "growing"
    elif spike_ratio >= 0.8:
        result["ccu_trend"] = "stable"
    else:
        result["ccu_trend"] = "declining"

    # Visit acceleration (second derivative)
    if len(visit_counts) >= 3:
        # First differences
        d1 = [visit_counts[i] - visit_counts[i-1] for i in range(1, len(visit_counts))]
        # Second difference (acceleration)
        if len(d1) >= 2:
            d2 = [d1[i] - d1[i-1] for i in range(1, len(d1))]
            result["visit_acceleration"] = round(sum(d2) / len(d2), 2)

    # Growth consistency: what fraction of intervals show positive growth
    if len(player_counts) >= 2:
        positive_intervals = sum(
            1 for i in range(1, len(player_counts))
            if player_counts[i] > player_counts[i-1]
        )
        result["growth_consistency"] = round(
            positive_intervals / (len(player_counts) - 1), 2
        )

    return result


def detect_spiking_games(
    game_ids: list[int],
    min_ccu: int = 50,
) -> list[dict]:
    """
    Scan a list of game IDs and return those that are currently spiking.

    A game is "spiking" if its CCU spike ratio exceeds the threshold
    and it has at least the minimum number of concurrent players.

    Returns list of dicts with game_id and velocity metrics.
    """
    spiking = []
    for gid in game_ids:
        velocity = compute_velocity(gid)
        if (velocity["ccu_trend"] == "spiking"
                and velocity["current_players"] >= min_ccu
                and velocity["snapshot_count"] >= _MIN_SNAPSHOTS):
            spiking.append({
                "game_id": gid,
                **velocity,
            })
    return spiking


def get_trending_score_from_velocity(velocity: dict) -> int:
    """
    Convert velocity metrics to a 0-100 trending score.

    Used to boost the Scout Score for games with strong momentum.
    """
    score = 0

    # CCU spike bonus (0-40)
    spike = velocity.get("ccu_spike", 1.0)
    if spike >= 3.0:
        score += 40
    elif spike >= 2.0:
        score += 30
    elif spike >= 1.5:
        score += 20
    elif spike >= 1.2:
        score += 10

    # Growth consistency bonus (0-30)
    consistency = velocity.get("growth_consistency", 0)
    if consistency >= 0.9:
        score += 30
    elif consistency >= 0.7:
        score += 20
    elif consistency >= 0.5:
        score += 10

    # Visit acceleration bonus (0-30)
    accel = velocity.get("visit_acceleration", 0)
    if accel >= 100000:
        score += 30
    elif accel >= 50000:
        score += 20
    elif accel >= 10000:
        score += 10
    elif accel >= 1000:
        score += 5

    return min(score, 100)