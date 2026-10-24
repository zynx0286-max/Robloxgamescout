"""Ascent Radar: early-breakout detection between scan cycles.

The contract under test (ascent.py):
* a candidate needs BOTH a spike (newest CCU vs avg of its other readings)
  and a window rise (newest vs lowest) — a steady climb or noise is never
  a candidate;
* the newest reading must be FRESH — stale data is the reason a game was
  not scanned lately, not proof it is still climbing;
* anti-spam: one post per game per cooldown, a per-cycle cap, and the
  post state stamped even when the channel is unresolved;
* a game the scanner already alerted on escalates instead of announcing.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ascent
import config
import database

ALERTED_GAME_ID = 3001  # scanner announced this one already


def _db(tmp_path, monkeypatch) -> str:
    """Point every module that owns the database path at a throwaway DB."""
    db_path = str(tmp_path / "ascent.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(ascent, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


def _seed(db_path, game_id, snapshots):
    """Insert (players, visits, 'offset') snapshots for one game."""
    conn = sqlite3.connect(db_path)
    for players, visits, offset in snapshots:
        conn.execute(
            "INSERT INTO game_history (game_id, players, visits, timestamp) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (game_id, players, visits, offset),
        )
    conn.commit()
    conn.close()


def _mark_alerted(db_path, game_id, name):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO alerted_games (game_id, game_name) VALUES (?, ?)",
        (game_id, name),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_spike_candidate_is_detected(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    # 30 CCU for two cycles, then a 111-CCU reading 2 minutes ago.
    _seed(db, 100, [
        (30, 5_000, "-50 minutes"),
        (40, 5_400, "-10 minutes"),
        (111, 6_000, "-2 minutes"),
    ])

    candidates = ascent.find_ascent_candidates(db_path=db)

    assert len(candidates) == 1
    c = candidates[0]
    assert c["game_id"] == 100
    assert c["latest_players"] == 111
    # baseline = avg(30, 40) = 35 → spike 3.17, rise 111/30 = 3.7
    assert c["spike_ratio"] == pytest.approx(3.17)
    assert c["rise_ratio"] == pytest.approx(3.7)


def test_steady_climb_is_not_a_candidate(tmp_path, monkeypatch):
    """A smooth rise is the scanner's job; the radar wants discontinuities."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 200, [
        (30, 5_000, "-50 minutes"),
        (60, 5_600, "-10 minutes"),
        (70, 6_000, "-2 minutes"),
    ])

    assert ascent.find_ascent_candidates(db_path=db) == []


def test_low_ccu_games_are_ignored(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 300, [(5, 100, "-50 minutes"), (6, 120, "-10 minutes"), (21, 200, "-2 minutes")])

    assert ascent.find_ascent_candidates(db_path=db) == []


def test_stale_latest_reading_is_not_a_candidate(tmp_path, monkeypatch):
    """A tall last reading from hours ago is not proof of a live climb."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 400, [
        (30, 5_000, "-20 hours"),
        (40, 5_400, "-10 hours"),
        (999, 9_000, "-3 hours"),  # meets every ratio, fails freshness
    ])

    assert ascent.find_ascent_candidates(db_path=db) == []


def test_candidates_sorted_by_spike_ratio(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 500, [(100, 1, "-30 minutes"), (110, 1, "-9 minutes"), (500, 1, "-2 minutes")])
    _seed(db, 501, [(100, 1, "-30 minutes"), (110, 1, "-9 minutes"), (220, 1, "-1 minutes")])

    found = [c["game_id"] for c in ascent.find_ascent_candidates(db_path=db)]

    assert found == [500, 501]  # 500/110 ≈ 4.55 beats 220/105 ≈ 2.10


# ---------------------------------------------------------------------------
# Names + escalation classification
# ---------------------------------------------------------------------------

def test_resolve_names_prefers_alerted_then_watched(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _mark_alerted(db, ALERTED_GAME_ID, "Ledger Name")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name, source) "
        "VALUES (0, 3002, 'Watched Name', 'auto')"
    )
    conn.commit()
    conn.close()

    candidates = [
        {"game_id": ALERTED_GAME_ID},
        {"game_id": 3002},
        {"game_id": 3099},
    ]
    ascent.resolve_names(db, candidates)

    assert candidates[0]["game_name"] == "Ledger Name"
    assert candidates[0]["was_alerted"] is True
    assert ascent.classify_kind(candidates[0]) == ascent.ESCALATE_KIND
    assert candidates[1]["game_name"] == "Watched Name"
    assert candidates[1]["was_alerted"] is False
    assert ascent.classify_kind(candidates[1]) == ascent.FIRST_FIRE_KIND
    assert candidates[2]["game_name"] == "Game 3099"


# ---------------------------------------------------------------------------
# Anti-spam
# ---------------------------------------------------------------------------

def test_cooldown_blocks_then_expires(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    state = {100: {"last_kind": "first", "last_alert_ts": "2026-01-01 12:00:00"}}

    import datetime as _dt
    from datetime import timezone

    now_10min_later = _dt.datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)
    now_60min_later = _dt.datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    candidate = {"game_id": 100}

    assert ascent.should_post_ascent(candidate, state, now=now_10min_later) is False
    assert ascent.should_post_ascent(candidate, state, now=now_60min_later) is True


def test_unknown_state_always_posts(tmp_path, monkeypatch):
    candidate = {"game_id": 100}
    assert ascent.should_post_ascent(candidate, {}) is True
    assert ascent.should_post_ascent(
        candidate, {100: {"last_kind": "first", "last_alert_ts": "garbage"}}
    ) is True


def test_mark_ascent_posted_inserts_then_updates(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    entry = {
        "game_id": 100,
        "game_name": "Rocket",
        "kind": "first",
        "latest_players": 200,
        "_now_str": "2026-01-01 12:00:00",
    }

    ascent.mark_ascent_posted(db, [entry])

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT game_name, last_kind, last_ccu, last_alert_ts "
        "FROM ascent_state WHERE game_id = 100"
    ).fetchone()
    conn.close()
    assert row == ("Rocket", "first", 200, "2026-01-01 12:00:00")

    entry2 = dict(entry, kind="escalate", latest_players=410,
                  _now_str="2026-01-01 12:50:00")
    ascent.mark_ascent_posted(db, [entry2])

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT last_kind, last_ccu, last_alert_ts FROM ascent_state "
        "WHERE game_id = 100"
    ).fetchall()
    conn.close()
    assert len(rows) == 1  # upsert, never a duplicate row
    assert rows[0] == ("escalate", 410, "2026-01-01 12:50:00")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def test_first_fire_text_announces_the_climb():
    text = ascent.build_ascent_text({
        "kind": "first", "game_name": "Rocket", "latest_players": 250,
        "spike_ratio": 3.2, "rise_ratio": 4.0,
    })
    assert "New ascent" in text and "Rocket" in text and "250" in text
    assert "Ascent continues" not in text


def test_escalate_text_reports_continuation():
    text = ascent.build_ascent_text({
        "kind": "escalate", "game_name": "Rocket", "latest_players": 800,
        "spike_ratio": 5.1, "rise_ratio": 8.0,
    })
    assert "Ascent continues" in text and "800" in text


# ---------------------------------------------------------------------------
# End-to-end loop body
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class _FakeBot:
    def __init__(self, channel, channel_id=555):
        self._channel = channel
        self._id = channel_id

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._id else None

    async def wait_until_ready(self):
        return None


def _wire_scheduler_like_host(monkeypatch, channel_id=555):
    """run_ascent_check reads ALERT_CHANNEL_ID inside the call — patch config."""
    monkeypatch.setattr(config, "ALERT_CHANNEL_ID", channel_id)


def test_full_tick_posts_and_stamps_state(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    _seed(db, 100, [(30, 5_000, "-50 minutes"), (40, 5_400, "-10 minutes"),
                    (111, 6_000, "-2 minutes")])
    _mark_alerted(db, ALERTED_GAME_ID, "Ledger Game")
    _seed(db, ALERTED_GAME_ID, [(100, 1, "-50 minutes"), (120, 1, "-10 minutes"),
                                (400, 1, "-2 minutes")])
    _wire_scheduler_like_host(monkeypatch)

    channel = _FakeChannel()
    posted = asyncio.run(ascent.run_ascent_check(_FakeBot(channel)))

    assert posted == 2
    bodies = channel.sent
    # Rocket is un-alerted → "New ascent"; the ledger game escalates.
    kinds = [("New ascent" in b) for b in bodies]
    escalations = [("Ascent continues" in b) for b in bodies]
    assert any(kinds) and any(escalations)
    # The alerted game's text carries its ledger name, not "Game 3001".
    assert any("Ledger Game" in b for b in bodies)

    conn = sqlite3.connect(db)
    stamped = dict(conn.execute(
        "SELECT game_id, last_kind FROM ascent_state"
    ).fetchall())
    conn.close()
    assert stamped == {100: "first", ALERTED_GAME_ID: "escalate"}

    # Re-running immediately posts nothing: the cooldown was stamped even
    # though posts succeeded (and NOTICE: it would also be stamped on a
    # failed post, so retry bursts never spam after a channel comes back).
    posted_again = asyncio.run(ascent.run_ascent_check(_FakeBot(channel)))
    assert posted_again == 0
    assert len(channel.sent) == 2


def test_cap_limits_alerts_per_cycle(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    for gid in range(4001, 4009):  # 8 candidate games
        _seed(db, gid, [(100, 1, "-30 minutes"), (110, 1, "-9 minutes"),
                        (300 + gid, 1, "-2 minutes")])
    _wire_scheduler_like_host(monkeypatch)

    posted = asyncio.run(ascent.run_ascent_check(_FakeBot(_FakeChannel())))

    assert posted == ascent.ASCENT_MAX_ALERTS_PER_CYCLE


def test_channelless_tick_still_stamps_state(tmp_path, monkeypatch):
    """A misconfigured channel must not queue a burst for later."""
    db = _db(tmp_path, monkeypatch)
    _seed(db, 100, [(30, 5_000, "-50 minutes"), (40, 5_400, "-10 minutes"),
                    (111, 6_000, "-2 minutes")])
    _wire_scheduler_like_host(monkeypatch, channel_id=555)

    posted = ascent_run = asyncio.run(
        ascent.run_ascent_check(_FakeBot(channel=None, channel_id=555))
    )

    assert posted == 0  # nothing could be sent
    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM ascent_state WHERE game_id = 100"
    ).fetchone()[0]
    conn.close()
    assert count == 1  # ... but the cooldown is already running
