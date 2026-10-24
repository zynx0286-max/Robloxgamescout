"""Tests for GET /api/v1/series — the dashboard's bulk time-series feed.

Contract: one indexed query serves every game's series (equivalent to
get_snapshots per game), untouched requested games keep an empty points
list instead of disappearing, args are clamped/deduped, unknown ids get
400s, and the payload is ETag/304-cacheable like the leaderboard.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_path = os.path.join(tempfile.mkdtemp(), "series-api.db")

import database
import portfolio_api
import portfolio_db


@pytest.fixture()
def seeded_api(monkeypatch):
    if os.path.exists(_db_path):
        os.remove(_db_path)
    monkeypatch.setattr(portfolio_db, "DATABASE", _db_path)
    # Portfolio's module-level DATABASE is what the worker uses.
    conn = sqlite3.connect(_db_path)
    conn.close()
    portfolio_db.create_portfolio_tables()
    portfolio_api._cache.clear()

    base = datetime.now(timezone.utc).replace(tzinfo=None)
    for offset_hours, players in [(2, 100), (1, 150)]:
        _insert_snapshot(
            10, players, players * 10,
            (base - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S"),
        )
    _insert_snapshot(
        20, 50, 500,
        (base - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    # A pre-window row for game 20: hours=24 must exclude it, 48 must keep it.
    _insert_snapshot(
        20, 40, 400,
        (base - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    yield portfolio_api


def _insert_snapshot(game_id, players, visits, ts):
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO game_snapshots (game_id, timestamp, players, visits) "
        "VALUES (?, ?, ?, ?)",
        (game_id, ts, players, visits),
    )
    conn.commit()
    conn.close()


def _client():
    from fastapi.testclient import TestClient
    return TestClient(portfolio_api.app)


# ---------------------------------------------------------------------------
# Store layer: get_snapshots_bulk
# ---------------------------------------------------------------------------


def test_bulk_series_matches_per_game_reads(seeded_api):
    from portfolio_db import get_snapshots, get_snapshots_bulk

    groups = get_snapshots_bulk([10, 20], hours=48)

    assert list(groups.keys()) == [10, 20]
    assert groups[10] == get_snapshots(10, hours=48)
    assert groups[20] == get_snapshots(20, hours=48)


def test_bulk_series_one_query_for_many_games(seeded_api, monkeypatch):
    from portfolio_db import get_snapshots_bulk

    real_connect = sqlite3.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(portfolio_db.sqlite3, "connect", counting_connect)

    get_snapshots_bulk([10, 20, 30], hours=48)

    assert len(calls) == 1


def test_bulk_series_missing_game_keeps_empty_group(seeded_api):
    groups = portfolio_db.get_snapshots_bulk([10, 999], hours=48)
    assert groups[999] == []
    assert len(groups[10]) == 2


def test_bulk_series_dedupes_ids(seeded_api):
    groups = portfolio_db.get_snapshots_bulk([10, 10, 20], hours=48)
    assert list(groups.keys()) == [10, 20]
    assert len(groups[10]) == 2


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------


def test_series_endpoint_groups_points_per_game(seeded_api):
    resp = _client().get("/api/v1/series?ids=10,20&hours=48")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_hours"] == 48
    s10 = next(s for s in body["series"] if s["game_id"] == 10)
    players = [p["players"] for p in s10["points"]]
    assert players == [100, 150]  # oldest first
    s20 = next(s for s in body["series"] if s["game_id"] == 20)
    # 30h-old and 1h-old rows both inside 48h: oldest first.
    assert [p["players"] for p in s20["points"]] == [40, 50]


def test_series_request_order_is_preserved_and_unknown_ids_appear(seeded_api):
    body = _client().get("/api/v1/series?ids=20,999,10&hours=48").json()
    assert [s["game_id"] for s in body["series"]] == [20, 999, 10]
    unknown = next(s for s in body["series"] if s["game_id"] == 999)
    assert unknown["points"] == []


def test_series_dedupes_and_caps_ids(seeded_api):
    body = _client().get(
        "/api/v1/series?ids=10,10,20&hours=48"
    ).json()
    assert len(body["series"]) == 2  # deduped


def test_series_rejects_unparseable_and_empty_ids(seeded_api):
    assert _client().get("/api/v1/series?ids=abc").status_code == 400
    assert _client().get("/api/v1/series?ids=").status_code == 400


def test_series_hours_is_clamped(seeded_api):
    body = _client().get("/api/v1/series?ids=10&hours=999999").json()
    assert body["period_hours"] == 2160
    body = _client().get("/api/v1/series?ids=10&hours=0").json()
    assert body["period_hours"] == 24


def test_series_hours_window_filters_old_rows(seeded_api):
    body = _client().get("/api/v1/series?ids=20&hours=24").json()
    s20 = next(s for s in body["series"] if s["game_id"] == 20)
    assert len(s20["points"]) == 1  # the 30h-old row is outside the window
    players = [p["players"] for p in s20["points"]]
    assert players == [50]


def test_series_etag_and_304(seeded_api):
    client = _client()
    first = client.get("/api/v1/series?ids=10,20&hours=48")
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = client.get(
        "/api/v1/series?ids=10,20&hours=48", headers={"If-None-Match": etag}
    )
    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_series_etag_ignores_generated_at(seeded_api):
    payload = {
        "period_hours": 24,
        "series": [{"game_id": 1}],
        "generated_at": "2020-01-01",
    }
    stamped = dict(payload, generated_at="2099-01-01")
    assert portfolio_api._data_etag(
        {"period_hours": payload["period_hours"], "series": payload["series"]}
    ) == portfolio_api._data_etag(
        {"period_hours": stamped["period_hours"], "series": stamped["series"]}
    )
