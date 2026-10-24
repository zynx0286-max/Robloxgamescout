"""Tests for GET /api/v1/leaderboard.

Serves the same alerted_games store /leaderboard reads, as JSON for the
Framer dashboard. Must be public (no auth — the data is already public in
the alert channel), cached, ordered by peak score, and clamped against
absurd query args.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_path = os.path.join(tempfile.mkdtemp(), "leaderboard-api.db")

import database
import portfolio_api


@pytest.fixture()
def seeded_api(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(database, "DATABASE", _db_path)
    conn = sqlite3.connect(_db_path)
    conn.close()
    database.create_database()
    portfolio_api._cache.clear()  # isolation between tests
    database.mark_alerted(10, "Gold Game", scout_score=95)
    database.mark_alerted(20, "Silver Game", scout_score=70)
    database.mark_alerted(30, "Repeat Game", scout_score=55)
    database.mark_alerted(30, "Repeat Game", scout_score=58)
    yield portfolio_api


def _client():
    from fastapi.testclient import TestClient
    return TestClient(portfolio_api.app)


def test_returns_entries_sorted_by_best_score(seeded_api):
    resp = _client().get("/api/v1/leaderboard")
    assert resp.status_code == 200
    body = resp.json()
    scores = [e["best_scout_score"] for e in body["entries"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 95


def test_payload_shape_and_repeat_count(seeded_api):
    body = _client().get("/api/v1/leaderboard").json()
    required = {"game_id", "name", "best_scout_score", "alert_count", "first_alerted"}
    for entry in body["entries"]:
        assert required.issubset(entry.keys())
    repeat = next(e for e in body["entries"] if e["game_id"] == 30)
    assert repeat["alert_count"] == 2
    assert repeat["best_scout_score"] == 58  # peak kept, not the latest


def test_window_days_filter(seeded_api):
    body = _client().get("/api/v1/leaderboard?days=0").json()
    assert body["window_days"] == 0
    assert len(body["entries"]) >= 3


def test_limit_is_clamped(seeded_api):
    body = _client().get("/api/v1/leaderboard?limit=9999").json()
    assert len(body["entries"]) <= 50


def test_empty_store_returns_empty_entries(monkeypatch):
    empty_db = os.path.join(tempfile.mkdtemp(), "leaderboard-empty.db")
    monkeypatch.setattr(database, "DATABASE", empty_db)
    conn = sqlite3.connect(empty_db)
    conn.close()
    database.create_database()
    portfolio_api._cache.clear()
    body = _client().get("/api/v1/leaderboard").json()
    assert body["entries"] == []


def test_endpoint_matches_the_command_store_for_the_same_args(seeded_api):
    """The JSON feed and /leaderboard read the same function."""
    from database import top_alerted_by_scout_score
    body = _client().get("/api/v1/leaderboard?limit=5&days=30").json()
    direct = top_alerted_by_scout_score(limit=5, days=30)
    assert [e["game_id"] for e in body["entries"]] == [r[0] for r in direct]


# ---------------------------------------------------------------------------
# ETag / conditional requests
# ---------------------------------------------------------------------------


def test_200_response_carries_etag_and_cache_headers(seeded_api):
    resp = _client().get("/api/v1/leaderboard")
    assert resp.status_code == 200
    assert resp.headers["etag"].startswith('"')
    assert "max-age=" in resp.headers["cache-control"]


def test_if_none_match_gets_a_304_with_no_body(seeded_api):
    client = _client()
    first = client.get("/api/v1/leaderboard")
    etag = first.headers["etag"]

    second = client.get("/api/v1/leaderboard", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content in (b"", b"null")
    assert second.headers["etag"] == etag


def test_weak_prefix_still_matches(seeded_api):
    client = _client()
    etag = client.get("/api/v1/leaderboard").headers["etag"]
    resp = client.get(
        "/api/v1/leaderboard", headers={"If-None-Match": f"W/{etag}"}
    )
    assert resp.status_code == 304


def test_list_of_etags_still_matches(seeded_api):
    client = _client()
    etag = client.get("/api/v1/leaderboard").headers["etag"]
    resp = client.get(
        "/api/v1/leaderboard",
        headers={"If-None-Match": f'"stale-etag", {etag}'},
    )
    assert resp.status_code == 304


def test_wrong_etag_gets_the_full_200(seeded_api):
    resp = _client().get(
        "/api/v1/leaderboard", headers={"If-None-Match": '"nope"'}
    )
    assert resp.status_code == 200
    assert "entries" in resp.json()


def test_etag_fingerprint_ignores_generated_at(seeded_api):
    """A rebuild landing the same ranking keeps the same ETag."""
    payload = {"window_days": 30, "entries": ["a"], "generated_at": "2026-01-01"}
    stamped = dict(payload, generated_at="2099-99-99")
    assert portfolio_api._leaderboard_etag(payload) == (
        portfolio_api._leaderboard_etag(stamped)
    )
    changed = dict(payload, entries=["b"])
    assert portfolio_api._leaderboard_etag(payload) != (
        portfolio_api._leaderboard_etag(changed)
    )
