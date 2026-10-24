"""Tests for Roblox request pacing, 429 backoff, and dead-endpoint removal.

Context: on a shared datacenter host, games.roblox.com returns HTTP 429
while apis.roblox.com stays healthy. Two things keep the bot under that
limit — pacing every request to that host behind a minimum gap, and not
spending requests at all on endpoints that 404.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import roblox_api_async as R


class _FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in returning canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if not self._responses:
            raise AssertionError(f"unexpected extra request: {url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _run(coro):
    return asyncio.run(coro)


class PacerTests(unittest.TestCase):
    def setUp(self):
        R.reset_rate_limiters()

    def tearDown(self):
        R.reset_rate_limiters()

    def test_pacer_spaces_consecutive_acquires(self):
        pacer = R._Pacer(0.05)
        loop = asyncio.new_event_loop()
        try:
            start = loop.time()
            for _ in range(4):
                loop.run_until_complete(pacer.acquire())
            elapsed = loop.time() - start
        finally:
            loop.close()
        # 4 acquires => 3 gaps of >=50ms.
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertEqual(pacer.waits, 3)

    def test_pacer_is_a_noop_when_interval_is_zero(self):
        loop = asyncio.new_event_loop()
        try:
            start = loop.time()
            for _ in range(5):
                loop.run_until_complete(R._Pacer(0).acquire())
            self.assertLess(loop.time() - start, 0.05)
        finally:
            loop.close()

    def test_chunks_splits_evenly_and_keeps_remainder(self):
        self.assertEqual(list(R._chunks([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])
        self.assertEqual(list(R._chunks([], 10)), [])


class RequestRetryTests(unittest.TestCase):
    def setUp(self):
        R.reset_rate_limiters()
        self.saved_interval = R.ROBLOX_API_MIN_INTERVAL
        # Pacing off so the tests measure retry behavior, not wall clock.
        R.ROBLOX_API_MIN_INTERVAL = 0

    def tearDown(self):
        R.reset_rate_limiters()
        R.ROBLOX_API_MIN_INTERVAL = self.saved_interval

    def test_429_is_retried_and_can_succeed(self):
        session = _FakeSession([
            _FakeResponse(429, None, {"Retry-After": "0"}),
            _FakeResponse(200, {"data": [{"universeId": 1, "name": "A"}]}),
        ])
        with patch("roblox_api_async.asyncio.sleep") as sleep:
            status, data = _run(
                R.request_json(session, "http://x/games", retries=3)
            )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"][0]["name"], "A")
        self.assertEqual(len(session.urls), 2)
        sleep.assert_called()

    def test_429_gives_up_after_the_retry_budget(self):
        session = _FakeSession([_FakeResponse(429) for _ in range(10)])
        with patch("roblox_api_async.asyncio.sleep"):
            status, data = _run(
                R.request_json(session, "http://x/games", retries=2)
            )
        self.assertEqual(status, 429)
        self.assertIsNone(data)
        # Initial attempt plus 2 retries, then it stops asking.
        self.assertEqual(len(session.urls), 3)

    def test_retry_after_header_is_honoured(self):
        R._Pacer(0)
        session = _FakeSession([
            _FakeResponse(429, None, {"Retry-After": "7"}),
            _FakeResponse(200, {"data": []}),
        ])
        with patch("roblox_api_async.asyncio.sleep") as sleep:
            _run(R.request_json(session, "http://x/games", retries=1))
        self.assertTrue(
            any(call.args[0] >= 7 for call in sleep.call_args_list),
            f"expected a >=7s wait, got {[c.args[0] for c in sleep.call_args_list]}",
        )

    def test_404_is_not_retried(self):
        session = _FakeSession([_FakeResponse(404), _FakeResponse(200)])
        status, data = _run(R.request_json(session, "http://x/games", retries=3))
        self.assertEqual(status, 404)
        self.assertIsNone(data)
        self.assertEqual(len(session.urls), 1)

    def test_client_error_returns_status_without_raising(self):
        session = _FakeSession([_FakeResponse(200), _FakeResponse(200)])
        with patch("roblox_api_async.asyncio.sleep"):
            status, data = _run(
                R.request_json(session, "http://x/games", retries=1)
            )
        self.assertEqual(status, 200)
        self.assertIsNone(data, "unparseable JSON must not raise")


class DeadEndpointTests(unittest.TestCase):
    """The three per-game endpoints the enrich path used are all 404 now.

    Each one was a request spent against an already-throttled IP for data
    that /v1/games already returns.
    """

    def test_description_comes_from_the_batch_payload(self):
        game = {"description": "  Join our Discord!  "}
        self.assertEqual(R.fetch_game_description(game), "Join our Discord!")

    def test_missing_or_blank_description_is_none(self):
        self.assertIsNone(R.fetch_game_description({}))
        self.assertIsNone(R.fetch_game_description({"description": "   "}))

    def test_creator_details_come_from_the_batch_payload(self):
        game = {"creator_id": 42, "creator_type": "Group", "creator": "Studio"}
        self.assertEqual(R.fetch_creator_details(game), (42, "Group", "Studio"))

    def test_creator_details_none_when_absent(self):
        self.assertEqual(R.fetch_creator_details({}), (None, None, None))

    def test_normalize_batch_carries_description_and_creator(self):
        payload = [{
            "universeId": 7,
            "name": "Game",
            "rootPlaceId": 99,
            "playing": 5,
            "visits": 10,
            "favoritedCount": 1,
            "description": "desc here",
            "creator": {"id": 3, "name": "C", "type": "Group"},
        }]
        game = R._normalize_batch(payload, [7])[7]
        self.assertEqual(game["description"], "desc here")
        self.assertEqual(game["creator_id"], 3)
        self.assertEqual(game["creator_type"], "Group")
        self.assertEqual(game["creator"], "C")

    def test_normalize_batch_tolerates_missing_creator(self):
        payload = [{"universeId": 8, "name": "Game"}]
        game = R._normalize_batch(payload, [8])[8]
        self.assertIsNone(game["creator_id"])
        self.assertEqual(game["description"], "")

    def test_enrich_description_makes_no_http_call(self):
        session = _FakeSession([])  # any request here fails the test
        game = {"id": 1, "description": "d"}
        result = _run(R.enrich_game_with_description(session, game))
        self.assertEqual(result["description"], "d")
        self.assertEqual(session.urls, [])


class BulkEnrichmentTests(unittest.TestCase):
    def setUp(self):
        R.reset_rate_limiters()
        self.saved_interval = R.ROBLOX_API_MIN_INTERVAL
        R.ROBLOX_API_MIN_INTERVAL = 0

    def tearDown(self):
        R.reset_rate_limiters()
        R.ROBLOX_API_MIN_INTERVAL = self.saved_interval

    def test_ratings_use_one_request_for_many_games(self):
        ids = list(range(1, 26))
        games = [{"id": i} for i in ids]
        session = _FakeSession([_FakeResponse(200, {
            "data": [{"id": i, "upVotes": 90, "downVotes": 10} for i in ids]
        })])
        _run(R.enrich_games_with_ratings(session, games))

        self.assertEqual(len(session.urls), 1, "25 games must cost 1 request")
        self.assertEqual(games[0]["rating_percent"], 90.0)
        self.assertEqual(games[0]["total_votes"], 100)

    def test_games_without_ratings_get_zero_percent(self):
        games = [{"id": 1}]
        session = _FakeSession([_FakeResponse(200, {"data": []})])
        _run(R.enrich_games_with_ratings(session, games))
        self.assertEqual(games[0]["rating_percent"], 0.0)

    def test_thumbnails_use_one_request_for_many_games(self):
        ids = list(range(1, 26))
        games = [{"id": i} for i in ids]
        session = _FakeSession([_FakeResponse(200, {
            "data": [{"targetId": i, "imageUrl": f"http://img/{i}"} for i in ids]
        })])
        _run(R.enrich_games_with_thumbnails(session, games))

        self.assertEqual(len(session.urls), 1)
        self.assertIn("thumbnails.roblox.com", session.urls[0])
        self.assertNotIn("games.roblox.com", session.urls[0])
        self.assertEqual(games[3]["thumbnail_url"], "http://img/4")

    def test_duplicate_ids_are_collapsed_into_one_request(self):
        games = [{"id": 5}, {"id": 5}, {"id": 5}]
        session = _FakeSession([_FakeResponse(200, {
            "data": [{"targetId": 5, "imageUrl": "http://img/5"}]
        })])
        _run(R.enrich_games_with_thumbnails(session, games))
        self.assertEqual(session.urls[0].count("universeIds=5"), 1)
        for g in games:
            self.assertEqual(g["thumbnail_url"], "http://img/5")

    def test_empty_input_makes_no_request(self):
        session = _FakeSession([])
        _run(R.enrich_games_with_ratings(session, []))
        _run(R.enrich_games_with_thumbnails(session, []))
        self.assertEqual(session.urls, [])


class DetailsBatchSizeTests(unittest.TestCase):
    """The details endpoint rejects oversized chunks with HTTP 400.

    Regression: /v1/games was batched at 100 ids, so every full chunk came
    back 400 and every game inside it silently disappeared from the scan —
    no alert, no error, nothing above a DEBUG log. Verified live: 50 ids is
    accepted, 60 is not.
    """

    def setUp(self):
        R.reset_rate_limiters()
        self.saved_interval = R.ROBLOX_API_MIN_INTERVAL
        R.ROBLOX_API_MIN_INTERVAL = 0

    def tearDown(self):
        R.reset_rate_limiters()
        R.ROBLOX_API_MIN_INTERVAL = self.saved_interval

    def test_details_chunk_size_stays_within_the_endpoint_limit(self):
        self.assertLessEqual(R._DETAILS_BATCH_SIZE, 50)

    def test_large_scans_are_chunked_within_the_limit(self):
        ids = list(range(1, 121))  # 120 ids -> 3 chunks of <= 50
        session = _FakeSession(
            [_FakeResponse(200, {"data": []}) for _ in range(3)]
        )

        _run(R.fetch_game_details_batch(session, ids))

        self.assertEqual(len(session.urls), 3)
        for url in session.urls:
            requested = url.split("universeIds=")[1].split("&")[0]
            self.assertLessEqual(len(requested.split(",")), 50)

    def test_a_rejected_chunk_is_logged_and_does_not_lose_the_others(self):
        ids = [1, 2, 3]
        session = _FakeSession([
            _FakeResponse(400, None),
            _FakeResponse(200, {"data": [{"universeId": 3, "name": "C"}]}),
        ])

        with patch.object(R, "_DETAILS_BATCH_SIZE", 2):
            with self.assertLogs("roblox_api_async", level="WARNING") as logs:
                results = _run(R.fetch_game_details_batch(session, ids))

        self.assertTrue(any("HTTP 400" in line for line in logs.output))
        # The rejected chunk yields None for its ids, but the good chunk still
        # comes through — a bad chunk must not abort the whole scan.
        self.assertEqual(results[2]["name"], "C")
        self.assertIsNone(results[0])
        self.assertIsNone(results[1])


if __name__ == "__main__":
    unittest.main()
