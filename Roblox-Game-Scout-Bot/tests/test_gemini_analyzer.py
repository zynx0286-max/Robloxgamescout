import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import gemini_analyzer


class GeminiAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        gemini_analyzer.DATABASE = self.temp_db.name
        os.environ["GEMINI_API_KEY"] = "test-key"
        # These tests target the Gemini path explicitly; keep the Groq
        # provider and AI_PROVIDER overrides out of the picture.
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("AI_PROVIDER", None)
        # _call_gemini retries transient 429/5xx with real backoff sleeps.
        # Most tests here simulate a hard failure and only care about the
        # fallback text, so pin it to a single attempt; the retry path
        # itself is covered by the tests further down.
        self._saved_attempts = gemini_analyzer.AI_MAX_ATTEMPTS
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        gemini_analyzer._ensure_table()

    def tearDown(self):
        gemini_analyzer.AI_MAX_ATTEMPTS = self._saved_attempts
        os.environ.pop("AI_PROVIDER", None)
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_answer_question_handles_http_429(self):
        class DummyResponse:
            status_code = 429
            text = '{"error": {"message": "quota exceeded"}}'

        with patch("gemini_analyzer.requests.post", return_value=DummyResponse()):
            result = gemini_analyzer.answer_question("hello there", force_refresh=True)

        self.assertIn("rate-limited", result.lower())
        self.assertIn("quota", result.lower())

    # ---- regression tests: fallback behavior must not poison the cache ----

    @staticmethod
    def _sample_game():
        return {"id": 12345, "name": "Test Game", "playing": 100, "visits": 1000}

    def test_model_404_reports_model_problem_not_missing_key(self):
        """A 404 (bad model name) must not read as "key not configured"."""
        class DummyResponse:
            status_code = 404
            text = '{"error": {"code": 404, "message": "Model not found"}}'

        with patch("gemini_analyzer.requests.post", return_value=DummyResponse()):
            result = gemini_analyzer.analyze_game(self._sample_game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_FALLBACK)
        joined = " ".join(result["risks"]) + result["recommendation"]
        self.assertIn("404", joined)
        self.assertNotIn("not enabled", joined.lower())

    def test_fallback_result_is_not_cached(self):
        """A failed Gemini call must not freeze the fallback into the 24h cache."""
        class DummyResponse:
            status_code = 500
            text = "boom"

        with patch("gemini_analyzer.requests.post", return_value=DummyResponse()):
            gemini_analyzer.analyze_game(self._sample_game())

        self.assertIsNone(gemini_analyzer._cache_get(12345))

    def test_quota_blocked_result_is_not_cached(self):
        """Quota-gate results must stay out of the cache too."""
        with patch.object(gemini_analyzer, "quota_remaining", return_value=0):
            result = gemini_analyzer.analyze_game(self._sample_game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_QUOTA)
        self.assertIsNone(gemini_analyzer._cache_get(12345))

    def test_successful_analysis_is_cached(self):
        """Real analyses still get cached — only failures skip the cache."""
        raw = {
            "verdict": gemini_analyzer.VERDICT_WORTH,
            "confidence": 80,
            "strengths": ["growing"],
            "risks": ["niche"],
            "recommendation": "watch it",
        }

        class DummyResponse:
            status_code = 200

            def json(self):
                return {
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps(raw)}]}}
                    ]
                }

        with patch("gemini_analyzer.requests.post", return_value=DummyResponse()):
            result = gemini_analyzer.analyze_game(self._sample_game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_WORTH)
        cached = gemini_analyzer._cache_get(12345)
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], gemini_analyzer.VERDICT_WORTH)


    # ---- retry + model-fallback behavior ----
    # A free-tier Gemini key returns "503 high demand" on a large share of
    # calls right now. Without retry+fallback, most games silently fall back
    # to the "Data Only" verdict, which is what the AI analysis exists to
    # prevent.

    @staticmethod
    def _json_response(payload, status=200):
        class DummyResponse:
            def __init__(self):
                self.status_code = status
                self.text = json.dumps(payload)

            def json(self):
                return payload

        return DummyResponse()

    @staticmethod
    def _gemini_ok_response(verdict):
        return GeminiAnalyzerTests._json_response({
            "candidates": [
                {"content": {"parts": [{"text": json.dumps({
                    "verdict": verdict,
                    "confidence": 70,
                    "strengths": ["a"],
                    "risks": ["b"],
                    "recommendation": "c",
                })}]}}
            ]
        })

    @staticmethod
    def _status_response(status):
        class DummyResponse:
            def __init__(self):
                self.status_code = status
                self.text = f'{{"error": {{"code": {status}}}}}'

            def json(self):
                return {"error": {"code": status}}

        return DummyResponse()

    def test_retryable_statuses_exclude_client_errors(self):
        """Only transient statuses retry — a 404/401 must fail fast."""
        for status in (429, 500, 502, 503, 504):
            self.assertIn(status, gemini_analyzer._RETRYABLE_STATUS)
        for status in (400, 401, 403, 404):
            self.assertNotIn(status, gemini_analyzer._RETRYABLE_STATUS)

    def test_transient_503_is_retried_until_it_succeeds(self):
        gemini_analyzer.AI_MAX_ATTEMPTS = 4
        wanted = gemini_analyzer.VERDICT_WORTH
        responses = [
            self._status_response(503),
            self._status_response(503),
            self._gemini_ok_response(wanted),
        ]
        with patch("gemini_analyzer.requests.post", side_effect=responses) as post, \
             patch("gemini_analyzer.time.sleep") as sleep:
            result = gemini_analyzer.analyze_game(
                self._sample_game(), force_refresh=True
            )

        self.assertEqual(post.call_count, 3)
        self.assertGreaterEqual(sleep.call_count, 2, "backoff sleep must be used")
        self.assertEqual(result["verdict"], wanted)

    def test_falls_back_to_another_model_when_primary_is_throttled(self):
        """A busy primary model must degrade to a working one, not to nothing."""
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        saved_models = gemini_analyzer.GEMINI_FALLBACK_MODELS
        gemini_analyzer.GEMINI_FALLBACK_MODELS = ["gemini-3.1-flash-lite"]
        try:
            wanted = gemini_analyzer.VERDICT_WAIT
            responses = [
                self._status_response(503),          # primary
                self._gemini_ok_response(wanted),    # fallback
            ]
            with patch(
                "gemini_analyzer.requests.post", side_effect=responses
            ) as post, patch("gemini_analyzer.time.sleep"):
                result = gemini_analyzer.analyze_game(
                    self._sample_game(), force_refresh=True
                )

            self.assertEqual(result["verdict"], wanted)
            self.assertEqual(post.call_count, 2)
            self.assertIn(
                "gemini-3.1-flash-lite", post.call_args_list[1][0][0]
            )
        finally:
            gemini_analyzer.GEMINI_FALLBACK_MODELS = saved_models

    def test_bad_api_key_does_not_walk_the_fallback_models(self):
        """401/403 is a key problem — no other model can fix it."""
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        saved_models = gemini_analyzer.GEMINI_FALLBACK_MODELS
        gemini_analyzer.GEMINI_FALLBACK_MODELS = ["gemini-3.1-flash-lite"]
        try:
            with patch(
                "gemini_analyzer.requests.post",
                return_value=self._status_response(401),
            ) as post, patch("gemini_analyzer.time.sleep"):
                result = gemini_analyzer.analyze_game(
                    self._sample_game(), force_refresh=True
                )

            self.assertEqual(post.call_count, 1)
            self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_FALLBACK)
            self.assertIn("401", " ".join(result["risks"]))
        finally:
            gemini_analyzer.GEMINI_FALLBACK_MODELS = saved_models

    # ---- provider chain: Groq primary, Gemini backup ----

    def test_provider_chain_prefers_groq_then_gemini(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        try:
            self.assertEqual(
                gemini_analyzer._provider_chain(), ["groq", "gemini"]
            )
            self.assertEqual(gemini_analyzer._active_provider(), "groq")
            self.assertIn("backup", gemini_analyzer.provider_status())
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_provider_chain_is_gemini_only_when_groq_absent(self):
        self.assertEqual(gemini_analyzer._provider_chain(), ["gemini"])

    def test_forced_provider_pins_the_chain_to_one(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        os.environ["AI_PROVIDER"] = "gemini"
        try:
            self.assertEqual(gemini_analyzer._provider_chain(), ["gemini"])
            self.assertIn("forced", gemini_analyzer.provider_status())
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_no_keys_means_no_chain(self):
        os.environ.pop("GEMINI_API_KEY", None)
        self.assertEqual(gemini_analyzer._provider_chain(), [])
        self.assertIsNone(gemini_analyzer._active_provider())

    def test_throttled_groq_hands_off_to_gemini(self):
        """The whole point of the backup: a dead primary is not a dead feature."""
        os.environ["GROQ_API_KEY"] = "groq-key"
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        wanted = gemini_analyzer.VERDICT_RISKY

        class GroqRateLimited:
            status_code = 429
            text = '{"error": {"message": "rate limit"}}'

            def json(self):
                return {"error": {"message": "rate limit"}}

        responses = [GroqRateLimited(), self._gemini_ok_response(wanted)]
        try:
            with patch(
                "gemini_analyzer.requests.post", side_effect=responses
            ) as post, patch("gemini_analyzer.time.sleep"):
                result = gemini_analyzer.analyze_game(
                    self._sample_game(), force_refresh=True
                )
            self.assertEqual(result["verdict"], wanted)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(gemini_analyzer._LAST_AI_PROVIDER, "gemini")
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_gemini_hands_back_to_groq_when_pinned_primary_is_pinned(self):
        """AI_PROVIDER=gemini stays pinned to Gemini, no sneaky fallback."""
        os.environ["GROQ_API_KEY"] = "groq-key"
        os.environ["AI_PROVIDER"] = "gemini"
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        try:
            with patch(
                "gemini_analyzer.requests.post",
                return_value=self._status_response(503),
            ) as post, patch("gemini_analyzer.time.sleep"):
                gemini_analyzer.analyze_game(
                    self._sample_game(), force_refresh=True
                )
            urls = [c[0][0] for c in post.call_args_list]
            self.assertTrue(urls)
            self.assertTrue(all("generativelanguage" in u for u in urls))
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_freeform_question_also_walks_the_chain(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        gemini_analyzer.AI_MAX_ATTEMPTS = 1

        class GroqRateLimited:
            status_code = 429
            text = "rate limit"

            def json(self):
                return {}

        class GeminiAnswer:
            status_code = 200

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "hi there"}]}}]}

        try:
            with patch(
                "gemini_analyzer.requests.post",
                side_effect=[GroqRateLimited(), GeminiAnswer()],
            ) as post, patch("gemini_analyzer.time.sleep"):
                answer, note = gemini_analyzer._call_provider_text("hello")
            self.assertEqual(answer, "hi there")
            self.assertIsNone(note)
            self.assertEqual(post.call_count, 2)
        finally:
            os.environ.pop("GROQ_API_KEY", None)

    def test_generation_config_reserves_room_for_thinking_tokens(self):
        """Gemini aliases now resolve to thinking models; cap the output so
        the answer text cannot be squeezed out by reasoning tokens."""
        gemini_analyzer.AI_MAX_ATTEMPTS = 1
        with patch(
            "gemini_analyzer.requests.post",
            return_value=self._gemini_ok_response(gemini_analyzer.VERDICT_WORTH),
        ) as post, patch("gemini_analyzer.time.sleep"):
            gemini_analyzer._call_gemini("hi")

        config = post.call_args[1]["json"]["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertGreaterEqual(config["maxOutputTokens"], 2048)


if __name__ == "__main__":
    unittest.main()
