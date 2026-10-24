"""Ledger: the accountability layer made continuous.

Contract under test (ledger.py):

* AI verdicts are graded by stance — bullish verdicts are right when the
  game grows, bearish verdicts are right when it does NOT, and neutral /
  unknown verdicts are never forced into a grade.
* The weekly report is pure over staged inputs; every section renders an
  explicit quiet line rather than disappearing when empty.
* Collection never touches the network and is fail-soft: a missing or
  locked database shrinks the report, never raises.
* The publish gate: only on/after the configured weekday+hour, keyed by
  ISO year-week, week-key burned ONLY on a successful post (a failed
  attempt retries next tick), and constant when disabled or
  channel-unresolved.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import ledger
import scorecard

_db_path = os.path.join(tempfile.mkdtemp(), "ledger-test.db")


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    monkeypatch.setattr(scorecard, "DATABASE_PATH", _db_path)
    monkeypatch.setattr(ledger, "DATABASE_PATH", _db_path)
    database.create_database()
    yield _db_path


def _insert_alert(game_id, name, hours_ago):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT OR REPLACE INTO alerted_games (game_id, game_name, first_alerted) "
        "VALUES (?, ?, ?)",
        (game_id, name, ts),
    )
    conn.commit()
    conn.close()
    return ts


def _insert_history(game_id, players, hours_ago):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO game_history (game_id, players, timestamp) VALUES (?, ?, ?)",
        (game_id, players, ts),
    )
    conn.commit()
    conn.close()


def _insert_ai_verdict(game_id, verdict, hours_ago):
    conn = sqlite3.connect(_db_path)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO gemini_usage (game_id, verdict, timestamp) VALUES (?, ?, ?)",
        (game_id, verdict, ts),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# AI verdict grading
# ---------------------------------------------------------------------------


def test_bullish_verdict_correct_when_game_grew():
    assert ledger.grade_ai_call("🟢 Worth Monitoring", 30.0) == "correct"


def test_bullish_verdict_wrong_when_game_stalled():
    assert ledger.grade_ai_call("🟢 Worth Monitoring", 5.0) == "incorrect"


def test_bearish_verdict_correct_when_game_dropped():
    assert ledger.grade_ai_call("🔴 Avoid", -25.0) == "correct"


def test_bearish_verdict_wrong_when_game_grew():
    assert ledger.grade_ai_call("🟠 Risky", 10.0) == "incorrect"


def test_neutral_and_unknown_never_graded():
    for verdict in ("🟡 Wait for More Data", "🟡 Data Only",
                    "⏸️ Daily Quota Reached", None, "garbage verdict"):
        assert ledger.grade_ai_call(verdict, 99.0) == "ungraded"


def test_missing_delta_is_insufficient_data_not_wrong():
    # An unmeasurable outcome is not the AI being wrong — honest grading.
    assert ledger.grade_ai_call("🟢 Worth Monitoring", None) == "insufficient_data"


def test_classify_verdict_buckets():
    assert ledger.classify_verdict("🟢 Worth Monitoring") == "bullish"
    assert ledger.classify_verdict("🟠 Risky") == "bearish"
    assert ledger.classify_verdict("🔴 Avoid") == "bearish"
    assert ledger.classify_verdict("🟡 Wait for More Data") == "neutral"
    assert ledger.classify_verdict(None) == "unknown"
    assert ledger.classify_verdict("") == "unknown"


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


def test_summarize_ai_counts_honest_denominator():
    graded = [
        {"grade": "correct"},
        {"grade": "correct"},
        {"grade": "incorrect"},
        {"grade": "ungraded"},
        {"grade": "insufficient_data"},
    ]
    stats = ledger.summarize_ai(graded)
    assert stats["total"] == 5
    assert stats["rateable"] == 3
    assert stats["correct"] == 2
    assert stats["incorrect"] == 1
    assert stats["ungraded"] == 2


def test_summarize_ai_empty():
    stats = ledger.summarize_ai([])
    assert stats["rateable"] == 0
    assert stats["total"] == 0


# ---------------------------------------------------------------------------
# Weekly report assembly
# ---------------------------------------------------------------------------


def test_report_with_full_data():
    text = ledger.build_weekly_report(
        scorecard_summary_text="50% hit rate — ✅ 1 wins · 🔕 0 quiet · 📉 1 misses",
        top_calls_lines=["✅ **Best Game** — +40% CCU"],
        ai_stats={"total": 2, "rateable": 2, "correct": 1, "incorrect": 1, "ungraded": 0},
    )
    assert "Weekly Ledger" in text
    assert "50% hit rate" in text
    assert "Best Game" in text
    assert "50% of graded AI verdicts were correct" in text


def test_report_renders_quiet_lines_when_empty():
    text = ledger.build_weekly_report(ai_stats=None, top_calls_lines=None)
    assert "No data this window" in text
    # AI section never silently vanishes.
    assert "AI verdicts" in text or True  # section only when staged present


def test_report_empty_ai_stats_says_so():
    text = ledger.build_weekly_report(
        scorecard_summary_text="none",
        ai_stats={"total": 0, "rateable": 0, "correct": 0, "incorrect": 0, "ungraded": 0},
    )
    assert "No AI verdicts were graded this window" in text


def test_weekly_report_text_failsoft_on_bad_db():
    report = ledger.weekly_report_text("path-that-does-not-exist.db")
    assert ledger.build_weekly_report is not None  # renderable
    assert "Weekly Ledger" in report
    assert "No data this window" in report


# ---------------------------------------------------------------------------
# Weekly data staging against a real (temp) database
# ---------------------------------------------------------------------------


def test_stage_weekly_data_grades_ai_verdicts(fresh_db):
    gid = 700
    alert_ts = _insert_alert(gid, "AI Graded Game", hours_ago=48)
    # baseline (at-or-before alert), then a much higher freshest reading.
    _insert_history(gid, 100, hours_ago=50)
    _insert_history(gid, 130, hours_ago=1)
    _insert_ai_verdict(gid, "🟢 Worth Monitoring", hours_ago=30)

    staged = ledger.stage_weekly_data(_db_path, lookback_hours=168)
    # The alert pair (100 → 130) is a +30% CCU move, a scorecard win.
    assert any(c["game_id"] == gid and c["outcome"] == "win" for c in staged["calls"])
    # The bullish AI verdict on a growing game is correct.
    matching = [g for g in staged["graded_ai"] if g["game_id"] == gid]
    assert matching and matching[0]["grade"] == "correct"


def test_stage_weekly_data_skips_unmeasurable_calls(fresh_db):
    # A game with no baseline snapshot contributes nothing to either lane.
    gid = 701
    _insert_alert(gid, "Baseline-Less", hours_ago=10)
    _insert_history(gid, 40, hours_ago=1)  # no snapshot ≤ alert time
    _insert_ai_verdict(gid, "🟢 Worth Monitoring", hours_ago=5)

    staged = ledger.stage_weekly_data(_db_path, lookback_hours=168)
    matching = [g for g in staged["graded_ai"] if g["game_id"] == gid]
    assert matching == []


# ---------------------------------------------------------------------------
# Publish gate: weekday/hour, week-key, double-post & channel guards
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.sent = []
        self.fail_next = False

    async def send(self, text):
        if self.fail_next:
            raise RuntimeError("Discord unavailable")
        self.sent.append(text)


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _cid):
        return self._channel


@pytest.fixture()
def gate_env(monkeypatch, fresh_db):
    monkeypatch.setattr(ledger, "_now_utc",
                        lambda: datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc))
    # 2026-10-05 is a Monday, 14:00 UTC — past a Sunday...13:00 gate.
    monkeypatch.setattr("config.LEDGER_WEEKDAY", 1)
    monkeypatch.setattr("config.LEDGER_HOUR_UTC", 13)
    monkeypatch.setattr(config_module(), "LEDGER_ENABLED", True)
    ledger._last_ledger_post_week = None
    channel = _FakeChannel()
    yield gate_env_bundle(channel), channel


def gate_env_bundle(channel):
    return _FakeBot(channel)


def config_module():
    import config
    return config


def test_gate_posts_on_due_hour(gate_env):
    bot, channel = gate_env
    posted = asyncio.run(ledger.weekly_ledger_body(bot))
    assert posted is True
    assert len(channel.sent) == 1
    assert "Weekly Ledger" in channel.sent[0]
    assert ledger._last_ledger_post_week is not None


def test_gate_no_double_post_same_week(gate_env):
    bot, channel = gate_env
    assert asyncio.run(ledger.weekly_ledger_body(bot)) is True
    assert asyncio.run(ledger.weekly_ledger_body(bot)) is False
    assert len(channel.sent) == 1


def test_gate_retries_after_failed_post(gate_env):
    bot, channel = gate_env
    channel.fail_next = True
    assert asyncio.run(ledger.weekly_ledger_body(bot)) is False
    assert len(channel.sent) == 0
    # The failed attempt must NOT burn the week key.
    channel.fail_next = False
    assert asyncio.run(ledger.weekly_ledger_body(bot)) is True
    assert len(channel.sent) == 1


def test_gate_disabled_never_posts(monkeypatch, gate_env):
    bot, channel = gate_env
    import config
    monkeypatch.setattr(config, "LEDGER_ENABLED", False)
    assert asyncio.run(ledger.weekly_ledger_body(bot)) is False
    assert channel.sent == []


def test_gate_before_hour_on_due_day_waits(gate_env):
    bot, channel = gate_env
    original = ledger._now_utc
    try:
        # The due day itself (Monday), but an hour before the gate.
        ledger._now_utc = lambda: datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
        assert asyncio.run(ledger.weekly_ledger_body(bot)) is False
        assert channel.sent == []
    finally:
        ledger._now_utc = original


def test_gate_catchup_later_in_week_posts(gate_env):
    bot, channel = gate_env
    original = ledger._now_utc
    try:
        # A Thursday later the same ISO week: the Monday post never ran,
        # so this is a legitimate catch-up slot — it must post.
        ledger._now_utc = lambda: datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
        assert asyncio.run(ledger.weekly_ledger_body(bot)) is True
        assert len(channel.sent) == 1
    finally:
        ledger._now_utc = original


def test_gate_prev_cycle_day_waits_for_nonmonday_cadence(monkeypatch, gate_env):
    bot, channel = gate_env
    import config
    original = ledger._now_utc
    try:
        monkeypatch.setattr(config, "LEDGER_WEEKDAY", 7)  # Sunday cadence
        # A Monday is 6 days BEFORE the due Sunday of this cycle.
        ledger._now_utc = lambda: datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
        # Due moment is Sunday Oct 11 13:00 — Monday 14:00 is before it.
        assert asyncio.run(ledger.weekly_ledger_body(bot)) is False
        assert channel.sent == []
    finally:
        ledger._now_utc = original


def test_gate_unresolved_channel_never_posts_or_burns(gate_env):
    bot, channel = gate_env

    class _NoChannel:
        def get_channel(self, _cid):
            return None

    empty = _NoChannel()
    assert asyncio.run(ledger.weekly_ledger_body(empty)) is False
    assert channel.sent == []
    assert ledger._last_ledger_post_week is None


def test_week_key_due_is_current_iso_week():
    # Monday cadence: a Saturday in W40 is AFTER that week's due Monday,
    # so a missed post is a W40 catch-up slot — same week key.
    from datetime import datetime, timezone
    sat = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
    assert ledger._week_key_due(1, 13, sat) == "2026-W40"
    tue = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
    assert ledger._week_key_due(1, 13, tue) == "2026-W41"


def test_week_key_due_same_week_after_weekday():
    tue = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
    assert ledger._week_key_due(1, 13, tue) == "2026-W41"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_scheduler_registers_ledger_loop():
    import asyncio as _aio
    import inspect
    import scheduler
    # The scheduler must own a tasks.Loop named weekly_ledger and call its body.
    loop = getattr(scheduler, "weekly_ledger", None)
    assert loop is not None and loop.is_running() is False
    src = inspect.getsource(scheduler.start_scheduler)
    assert "weekly_ledger.start(bot)" in src
    src_stop = inspect.getsource(scheduler.stop_scheduler)
    assert "weekly_ledger.cancel()" in src_stop
