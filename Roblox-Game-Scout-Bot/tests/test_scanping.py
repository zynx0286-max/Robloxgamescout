"""Tests for /scanping subscriptions and per-user ignore honoring."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "ping-test.db")

import database


@pytest.fixture()
def fresh_db(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    database.create_database()
    yield _db_path


def test_ping_subscription_roundtrip(fresh_db):
    database.add_user(111)
    assert database.get_user_filters(111)["ping_subscribed"] is False

    database.update_user(111, ping_subscribed=True)
    assert database.get_user_filters(111)["ping_subscribed"] is True
    assert database.get_pinged_subscribers() == [111]

    database.update_user(111, ping_subscribed=False)
    assert database.get_pinged_subscribers() == []


def test_ping_subscription_persists_after_other_updates(fresh_db):
    database.add_user(222)
    database.update_user(222, ping_subscribed=True)
    # A later unrelated settings update must not clobber the subscription.
    database.update_user(222, minimum_growth=10)
    assert database.get_pinged_subscribers() == [222]
    assert database.get_user_filters(222)["minimum_growth"] == 10


def test_is_user_ignoring(fresh_db):
    database.add_user(333)
    assert database.is_user_ignoring(333, 994732206) is False

    database.ignore_game_for_user(333, 994732206)
    assert database.is_user_ignoring(333, 994732206) is True
    assert database.is_user_ignoring(333, 1) is False
    assert database.is_user_ignoring(444, 994732206) is False


def test_subscribed_user_row_missing_column_defaults_off(fresh_db):
    # A fresh user row (created via add_user only) should read as not
    # subscribed and not break get_pinged_subscribers.
    database.add_user(555)
    assert database.get_pinged_subscribers() == []
