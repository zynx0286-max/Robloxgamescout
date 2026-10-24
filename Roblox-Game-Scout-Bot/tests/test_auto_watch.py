"""Auto-watch: system tracks for games the bot alerts on.

The feature exists because an alert is a one-time post — a game that looked
interesting at 2 PM was never looked at again. These tests pin the rules:

  * auto rows are owned by AUTO_WATCH_USER_ID (default 0 = the bot) and
    stamped source='auto';
  * the scheduler creates one after a posted alert;
  * the cap prunes only 'auto' rows, never a user's own watch;
  * the tracker posts system-watch alerts quietly, with no @-mention.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import database


def _db(tmp_path, monkeypatch) -> str:
    """Point every module that owns DATABASE_PATH at a throwaway database."""
    import chart_rank
    import history

    db_path = str(tmp_path / "autowatch.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(history, "DATABASE_PATH", db_path)
    monkeypatch.setattr(chart_rank, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


def _rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def test_source_column_is_added_to_legacy_tables(tmp_path, monkeypatch):
    """A watched_games table from before the feature must migrate in place."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE watched_games (
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
        """
    )
    conn.execute(
        "INSERT INTO watched_games (user_id, game_id, game_name) "
        "VALUES (123, 42, 'Legacy Watch')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(database, "DATABASE", str(db_path))
    database.create_database()

    # The pre-existing row was, by definition, created by a human.
    rows = _rows(db_path, "SELECT user_id, game_id, source FROM watched_games")
    assert rows == [(123, 42, "user")]


def test_auto_watch_creates_a_system_row(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 0)

    inserted = database.auto_watch_game(
        {"id": 42, "name": "Test Game", "playing": 100, "visits": 1000}
    )

    assert inserted is True
    rows = _rows(
        db,
        "SELECT user_id, game_id, source, last_players FROM watched_games",
    )
    assert rows == [(0, 42, "auto", 100)]


def test_auto_watch_is_idempotent(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 0)
    game = {"id": 42, "name": "Test Game", "playing": 100, "visits": 1000}

    assert database.auto_watch_game(game) is True
    # A second pass (game re-alerts are prevented elsewhere, but the bot may
    # restart) must not duplicate or refresh the row.
    assert database.auto_watch_game(game) is False

    import database as db_module
    assert database.count_auto_watches() == 1


def test_auto_watch_never_rewrites_a_human_watch(tmp_path, monkeypatch):
    """The same owner id watching both ways: the human row wins."""
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 123)

    database.watch_game_for_user(
        123, {"id": 42, "name": "Test Game", "playing": 50, "visits": 500}
    )
    database.auto_watch_game(
        {"id": 42, "name": "Test Game", "playing": 100, "visits": 1000},
        user_id=123,
    )

    rows = _rows(
        db, "SELECT last_players, source FROM watched_games WHERE user_id = 123"
    )
    assert rows == [(50, "user")]


def test_cap_prunes_only_auto_rows_and_never_user_rows(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 0)

    # A human watch that is older than every auto watch.
    database.watch_game_for_user(
        123, {"id": 1, "name": "Human Watch", "playing": 10, "visits": 10}
    )
    for game_id in range(2, 6):
        database.auto_watch_game(
            {"id": game_id, "name": f"Auto {game_id}", "playing": 10, "visits": 10}
        )

    deleted = database.prune_auto_watches(cap=2)

    # 4 auto rows trimmed to 2 (the two oldest gone), the user's row
    # untouched even though it is the oldest watch in the table.
    assert deleted == 2
    remaining = _rows(db, "SELECT game_id, source FROM watched_games ORDER BY id")
    assert remaining == [(1, "user"), (4, "auto"), (5, "auto")]
    assert database.count_auto_watches() == 2


def test_cap_zero_removes_all_auto_rows(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 0)

    database.auto_watch_game({"id": 42, "name": "G", "playing": 1, "visits": 1})
    database.watch_game_for_user(
        123, {"id": 99, "name": "Keep", "playing": 1, "visits": 1}
    )

    assert database.prune_auto_watches(cap=0) == 1
    rows = _rows(db, "SELECT user_id, source FROM watched_games")
    assert rows == [(123, "user")]


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None):
        self.sent.append((content, embed, view))


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _channel_id):
        return self._channel

    async def wait_until_ready(self):
        # Mirrors discord.Client: tests run after "readiness" by definition.
        return None


def test_scheduler_auto_watches_a_posted_alert(tmp_path, monkeypatch):
    """End to end: a posted alert creates one source='auto' watch row."""
    import scheduler

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "AUTO_WATCH_ALERTS", True)
    monkeypatch.setattr(config, "AUTO_WATCH_USER_ID", 0)
    monkeypatch.setattr(scheduler, "ALERT_CHANNEL_ID", 555)

    game = {
        "id": 4242,
        "name": "Alerted Game",
        "playing": 100,
        "visits": 1000,
        "rating_percent": 90.0,
        "place_id": 4242,
    }

    posted = []

    async def fake_post_alert(channel, g):
        posted.append(g["id"])

    async def fake_enrich(session, g):
        return g

    async def fake_scan(session=None):
        return [game]

    monkeypatch.setattr(scheduler, "_post_alert", fake_post_alert)
    monkeypatch.setattr(scheduler, "_enrich_game_data", fake_enrich)
    monkeypatch.setattr(scheduler, "scan_games_async", fake_scan)
    monkeypatch.setattr(scheduler, "analyze_game", lambda g, force_refresh=False: {})
    monkeypatch.setattr(scheduler, "is_alerted", lambda gid: False)
    monkeypatch.setattr(scheduler, "mark_alerted", lambda gid, name, scout_score=None: None)

    async def fake_tracker(bot):
        return 0

    monkeypatch.setattr(scheduler, "run_tracker", fake_tracker)

    channel = _FakeChannel()
    asyncio.run(scheduler._run_scan_once(_FakeBot(channel)))

    assert posted == [4242]
    rows = _rows(
        tmp_path / "autowatch.db",
        "SELECT user_id, game_id, source FROM watched_games",
    )
    assert rows == [(0, 4242, "auto")]


def test_failed_alert_post_is_not_marked_alerted(tmp_path, monkeypatch):
    """mark_alerted must happen only after a successful send.

    Marking first meant a failed channel.send permanently consumed the
    game's one-time alert: every later cycle saw it as a duplicate and
    nothing ever retried. After the fix the game stays un-alerted on a
    failed post, so the next scan cycle retries it.
    """
    import scheduler

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "AUTO_WATCH_ALERTS", False)
    monkeypatch.setattr(scheduler, "ALERT_CHANNEL_ID", 555)

    game = {
        "id": 4242,
        "name": "Retry Me",
        "playing": 100,
        "visits": 1000,
        "rating_percent": 90.0,
        "place_id": 4242,
    }

    async def failing_post(channel, g):
        raise RuntimeError("discord down")

    posted = []

    async def working_post(channel, g):
        posted.append(g["id"])

    async def fake_enrich(session, g):
        return g

    async def fake_scan(session=None):
        return [dict(game)]

    async def fake_tracker(bot):
        return 0

    monkeypatch.setattr(scheduler, "_enrich_game_data", fake_enrich)
    monkeypatch.setattr(scheduler, "scan_games_async", fake_scan)
    monkeypatch.setattr(scheduler, "analyze_game", lambda g, force_refresh=False: {})
    monkeypatch.setattr(scheduler, "run_tracker", fake_tracker)

    # Cycle 1: the post fails — the game must stay un-alerted.
    monkeypatch.setattr(scheduler, "_post_alert", failing_post)
    asyncio.run(scheduler._run_scan_once(_FakeBot(_FakeChannel())))
    assert database.is_alerted(4242) is False

    # Cycle 2: the post succeeds — now (and only now) it is marked.
    monkeypatch.setattr(scheduler, "_post_alert", working_post)
    asyncio.run(scheduler._run_scan_once(_FakeBot(_FakeChannel())))
    assert posted == [4242]
    assert database.is_alerted(4242) is True


def test_unresolved_channel_does_not_mark_alerted(tmp_path, monkeypatch):
    """No alert channel → matches must stay alert-eligible.

    With the channel unresolved, marking first silently swallowed every
    match: nothing was posted, yet the games were all flagged alerted, so
    fixing ALERT_CHANNEL_ID later still produced silence.
    """
    import scheduler

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "AUTO_WATCH_ALERTS", False)
    monkeypatch.setattr(scheduler, "ALERT_CHANNEL_ID", 555)

    game = {
        "id": 4243,
        "name": "No Channel Game",
        "playing": 100,
        "visits": 1000,
        "rating_percent": 90.0,
        "place_id": 4243,
    }

    async def fake_enrich(session, g):
        return g

    async def fake_scan(session=None):
        return [game]

    async def fake_tracker(bot):
        return 0

    monkeypatch.setattr(scheduler, "_enrich_game_data", fake_enrich)
    monkeypatch.setattr(scheduler, "scan_games_async", fake_scan)
    monkeypatch.setattr(scheduler, "analyze_game", lambda g, force_refresh=False: {})
    monkeypatch.setattr(scheduler, "run_tracker", fake_tracker)

    # get_channel resolves to None — the channel is not ready.
    asyncio.run(scheduler._run_scan_once(_FakeBot(None)))
    assert database.is_alerted(4243) is False


def test_tracker_alerts_on_a_collapse(tmp_path, monkeypatch):
    """A watched game collapsing -70% CCU fires a decline alert.

    The tracker used to be growth-only: a halving (or worse) was silence,
    despite the auto-watch docs promising decline reporting. A collapse is
    a decision-grade signal for an acquisition scout — positive or negative.
    """
    import tracker

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(tracker, "save_alert_log", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "update_watched_game_snapshots_bulk", lambda *a: None)
    monkeypatch.setattr(tracker, "get_user_alert_level", lambda uid: "all")

    async def fake_info(game_id):
        return {"playing": 30, "visits": 5300, "place_id": 42}

    monkeypatch.setattr(tracker, "get_game_info", fake_info)
    # Baseline 100 players -> 30 now = -70%; visits +4% (noise).
    monkeypatch.setattr(
        tracker,
        "get_all_watched_games",
        lambda: [(0, 42, "Dying Game", 100, 5000, "2026-10-01 00:00:00", "auto")],
    )

    channel = _FakeChannel()
    alerts = asyncio.run(tracker.run_tracker(_FakeBot(channel)))

    assert alerts == 1
    content, embed, _ = channel.sent[0]
    assert "DECLINING" in embed.title or "COLLAPSE" in embed.title
    assert "<@" not in content  # auto watch stays quiet


def test_tracker_decline_never_alerts_on_noise(tmp_path, monkeypatch):
    """A -20% wobble stays silent: decline thresholds are steeper than
    growth ones so ordinary CCU noise never reads as a collapse."""
    import tracker

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(tracker, "save_alert_log", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "update_watched_game_snapshots_bulk", lambda *a: None)

    async def fake_info(game_id):
        return {"playing": 80, "visits": 5100, "place_id": 42}

    monkeypatch.setattr(tracker, "get_game_info", fake_info)
    monkeypatch.setattr(
        tracker,
        "get_all_watched_games",
        lambda: [(0, 42, "Wobbly Game", 100, 5000, "2026-10-01 00:00:00", "auto")],
    )

    channel = _FakeChannel()
    alerts = asyncio.run(tracker.run_tracker(_FakeBot(channel)))
    assert alerts == 0
    assert channel.sent == []


def test_decline_unit_thresholds_and_priority():
    import tracker

    # Symmetric triggers
    assert tracker._should_alert_decline(-50, 0) is True
    assert tracker._should_alert_decline(0, -30) is True
    assert tracker._should_alert_decline(-49.9, 10) is False
    assert tracker._should_alert_decline(10, -29.9) is False
    assert tracker._should_alert_decline(50, 10) is False

    # A halving is never "low": medium at minimum, high on a collapse.
    assert tracker._classify_decline_priority(-50, 0) == tracker.PRIORITY_MEDIUM
    assert tracker._classify_decline_priority(0, -30) == tracker.PRIORITY_MEDIUM
    assert tracker._classify_decline_priority(-100, 0) == tracker.PRIORITY_HIGH
    assert tracker._classify_decline_priority(-40, -70) == tracker.PRIORITY_HIGH


def test_scheduler_does_not_auto_watch_when_disabled(tmp_path, monkeypatch):
    import scheduler

    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "AUTO_WATCH_ALERTS", False)
    monkeypatch.setattr(scheduler, "ALERT_CHANNEL_ID", 555)

    game = {
        "id": 4242,
        "name": "Alerted Game",
        "playing": 100,
        "visits": 1000,
        "rating_percent": 90.0,
        "place_id": 4242,
    }

    async def fake_post_alert(channel, g):
        return None

    async def fake_enrich(session, g):
        return g

    async def fake_scan(session=None):
        return [game]

    monkeypatch.setattr(scheduler, "_post_alert", fake_post_alert)
    monkeypatch.setattr(scheduler, "_enrich_game_data", fake_enrich)
    monkeypatch.setattr(scheduler, "scan_games_async", fake_scan)
    monkeypatch.setattr(scheduler, "analyze_game", lambda g, force_refresh=False: {})
    monkeypatch.setattr(scheduler, "is_alerted", lambda gid: False)
    monkeypatch.setattr(scheduler, "mark_alerted", lambda gid, name, scout_score=None: None)

    async def fake_tracker(bot):
        return 0

    monkeypatch.setattr(scheduler, "run_tracker", fake_tracker)

    asyncio.run(scheduler._run_scan_once(_FakeBot(_FakeChannel())))

    assert _rows(db, "SELECT * FROM watched_games") == []


def test_scheduler_logs_a_rejection_summary_when_nothing_matches(
    tmp_path, monkeypatch
):
    """The dominant-reason warning: an empty cycle must say WHY it was empty."""
    import scheduler

    db = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler, "ALERT_CHANNEL_ID", 555)

    # Every game fails the rating check — the exact signature of the old
    # "ratings fetched after the filters" bug.
    game = {
        "id": 4242,
        "name": "Unrated Game",
        "playing": 100,
        "visits": 1000,
        "rating_percent": 0,
        "place_id": 4242,
    }

    async def fake_enrich(session, g):
        return g

    async def fake_scan(session=None):
        return [game]

    monkeypatch.setattr(scheduler, "_enrich_game_data", fake_enrich)
    monkeypatch.setattr(scheduler, "scan_games_async", fake_scan)
    monkeypatch.setattr(scheduler, "is_alerted", lambda gid: False)

    async def fake_tracker(bot):
        return 0

    monkeypatch.setattr(scheduler, "run_tracker", fake_tracker)

    asyncio.run(scheduler._run_scan_once(_FakeBot(_FakeChannel())))

    summaries = _rows(
        db, "SELECT message FROM scan_log WHERE event = 'rejection_summary'"
    )
    assert len(summaries) == 1
    assert "Rating" in summaries[0][0]


def test_tracker_posts_system_watches_without_mentions(tmp_path, monkeypatch):
    """An auto-only watch alerts quietly; a human watcher still gets pinged."""
    import tracker

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(tracker, "save_alert_log", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "update_watched_game_snapshots_bulk", lambda *a: None)
    monkeypatch.setattr(tracker, "get_user_alert_level", lambda uid: "all")

    async def fake_info(game_id):
        return {"playing": 250, "visits": 5200, "place_id": 4242}

    monkeypatch.setattr(tracker, "get_game_info", fake_info)

    # A +150% CCU move: +100 baseline -> +250 now trips every threshold.
    rows = [
        (0, 42, "Auto Game", 100, 5000, "2026-10-01 00:00:00", "auto"),
        (123, 42, "Auto Game", 100, 5000, "2026-10-01 00:00:00", "user"),
    ]
    monkeypatch.setattr(tracker, "get_all_watched_games", lambda: rows)

    channel = _FakeChannel()
    alerts = asyncio.run(tracker.run_tracker(_FakeBot(channel)))

    # One fetch and one alert per GAME, even with two rows for it.
    assert alerts == 1
    assert len(channel.sent) == 1
    content, embed, _ = channel.sent[0]
    assert "<@123>" in content
    assert "<@0>" not in content


def test_tracker_auto_only_watch_is_quiet(tmp_path, monkeypatch):
    import tracker

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(tracker, "save_alert_log", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "update_watched_game_snapshots_bulk", lambda *a: None)

    async def fake_info(game_id):
        return {"playing": 250, "visits": 5200, "place_id": 4242}

    monkeypatch.setattr(tracker, "get_game_info", fake_info)
    monkeypatch.setattr(
        tracker,
        "get_all_watched_games",
        lambda: [(0, 42, "Auto Game", 100, 5000, "2026-10-01 00:00:00", "auto")],
    )

    channel = _FakeChannel()
    asyncio.run(tracker.run_tracker(_FakeBot(channel)))

    assert len(channel.sent) == 1
    content, embed, _ = channel.sent[0]
    assert "<@" not in content
    assert "Auto-watched" in content
    assert "/unwatch 42" in content


def test_tracker_fetches_once_per_game(tmp_path, monkeypatch):
    """Two users watching the same game cost one Roblox request, not two."""
    import tracker

    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(tracker, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(tracker, "save_alert_log", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "update_watched_game_snapshots_bulk", lambda *a: None)
    monkeypatch.setattr(tracker, "get_user_alert_level", lambda uid: "all")

    fetches = []

    async def fake_info(game_id):
        fetches.append(game_id)
        # No meaningful change: never trips the alert thresholds.
        return {"playing": 101, "visits": 5010, "place_id": 4242}

    monkeypatch.setattr(tracker, "get_game_info", fake_info)
    monkeypatch.setattr(
        tracker,
        "get_all_watched_games",
        lambda: [
            (123, 42, "Game", 100, 5000, "2026-10-01 00:00:00", "user"),
            (456, 42, "Game", 100, 5000, "2026-10-01 00:00:00", "user"),
        ],
    )

    channel = _FakeChannel()
    asyncio.run(tracker.run_tracker(_FakeBot(channel)))

    assert fetches == [42]
    assert channel.sent == []
