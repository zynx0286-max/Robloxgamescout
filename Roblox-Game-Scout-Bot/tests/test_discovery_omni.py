"""Tests for the omni-search discovery rebuild.

The legacy /v1/games/list and /v1/games/recommendations endpoints were
retired by Roblox (both 404), so keyword discovery now runs on
apis.roblox.com/search-api/omni-search. These tests lock in the payload
normalization and the multi-keyword merge without any network access.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roblox_search


OMNI_PAYLOAD = {
    "searchResults": [
        {
            "contentGroupType": "Game",
            "contents": [
                {
                    "universeId": 111,
                    "name": "Cool Game",
                    "playerCount": 454,
                    "totalUpVotes": 100,
                    "totalDownVotes": 10,
                    "creatorName": "Alice",
                    "rootPlaceId": 555,
                },
                {"universeId": 222, "name": "Other Game", "playerCount": 12},
                # Missing universeId -> skipped
                {"name": "Broken", "playerCount": 1},
            ],
        },
        # Non-game groups are ignored
        {"contentGroupType": "Creator", "contents": [{"universeId": 999}]},
    ]
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        payload = self.payloads.pop(0) if self.payloads else None
        return _FakeResponse(payload)


def test_normalize_omni_results_extracts_games_only():
    games = roblox_search._normalize_omni_results(OMNI_PAYLOAD, limit=50)

    assert [g["id"] for g in games] == [111, 222]
    assert games[0]["name"] == "Cool Game"
    assert games[0]["playing"] == 454
    assert games[0]["place_id"] == 555
    assert games[0]["source"] == "RobloxSearch"


def test_normalize_omni_results_respects_limit():
    games = roblox_search._normalize_omni_results(OMNI_PAYLOAD, limit=1)
    assert len(games) == 1


def test_normalize_omni_results_handles_empty_payload():
    assert roblox_search._normalize_omni_results({}, limit=10) == []
    assert roblox_search._normalize_omni_results({"searchResults": []}, limit=10) == []


def test_search_keyword_url_includes_session_and_query():
    session = _FakeSession([OMNI_PAYLOAD])

    async def _run():
        return await roblox_search.search_keyword(session, "simulator", limit=30)

    games = asyncio.run(_run())

    assert [g["id"] for g in games] == [111, 222]
    url = session.calls[0]
    assert "apis.roblox.com/search-api/omni-search" in url
    assert "searchQuery=simulator" in url
    # The API returns 200-but-empty without a sessionId.
    assert "sessionId=" in url


def test_multi_keyword_search_dedupes_and_caps():
    payload_a = {"searchResults": [{"contentGroupType": "Game", "contents": [
        {"universeId": 1, "name": "A"}, {"universeId": 2, "name": "B"}]}]}
    payload_b = {"searchResults": [{"contentGroupType": "Game", "contents": [
        {"universeId": 2, "name": "B-dup"}, {"universeId": 3, "name": "C"}]}]}
    session = _FakeSession([payload_a, payload_b])

    async def _run():
        return await roblox_search.multi_keyword_search(
            session, ["kw1", "kw2"], max_total=2
        )

    games = asyncio.run(_run())

    assert [g["id"] for g in games] == [1, 2]  # deduped and capped at max_total


def test_multi_keyword_search_survives_exceptions():
    session = _FakeSession([RuntimeError("boom"), OMNI_PAYLOAD])

    async def _run():
        return await roblox_search.multi_keyword_search(
            session, ["bad", "good"], max_total=10
        )

    games = asyncio.run(_run())
    assert [g["id"] for g in games] == [111, 222]
