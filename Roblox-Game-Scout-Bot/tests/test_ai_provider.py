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


class _JsonResponse:
    """Minimal requests.Response stand-in returning a JSON body."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _openai_ok(payload):
    return _JsonResponse(200, {"choices": [{"message": {"content": json.dumps(payload)}}]})


def _gemini_ok(payload):
    return _JsonResponse(
        200,
        {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
    )


class AiProviderSelectionTests(unittest.TestCase):
    """Provider dispatch: Groq primary when set, Gemini fallback, auto order."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        gemini_analyzer.DATABASE = self.temp_db.name
        gemini_analyzer._ensure_table()
        # Deterministic provider state for every test.
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("AI_PROVIDER", None)

    def tearDown(self):
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("AI_PROVIDER", None)
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    @staticmethod
    def _game():
        return {"id": 999, "name": "Provider Game", "playing": 50, "visits": 500}

    def test_no_keys_means_not_configured(self):
        self.assertIsNone(gemini_analyzer._active_provider())
        result = gemini_analyzer.analyze_game(self._game())
        joined = " ".join(result["risks"])
        self.assertIn("GROQ_API_KEY", joined)

    def test_auto_prefers_groq_when_both_keys_set(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        self.assertEqual(gemini_analyzer._active_provider(), "groq")

        raw = {
            "verdict": gemini_analyzer.VERDICT_WORTH,
            "confidence": 70,
            "strengths": ["s"],
            "risks": ["r"],
            "recommendation": "rec",
        }
        with patch(
            "gemini_analyzer.requests.post",
            return_value=_openai_ok(raw),
        ) as mock_post:
            result = gemini_analyzer.analyze_game(self._game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_WORTH)
        called_url = mock_post.call_args.args[0]
        self.assertIn("api.groq.com", called_url)
        self.assertIn("Bearer groq-key", mock_post.call_args.kwargs["headers"]["Authorization"])
        # Gemini must never be hit when Groq succeeds.
        self.assertEqual(mock_post.call_count, 1)

    def test_auto_falls_back_to_gemini_without_groq_key(self):
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        self.assertEqual(gemini_analyzer._active_provider(), "gemini")

        raw = {
            "verdict": gemini_analyzer.VERDICT_WAIT,
            "confidence": 40,
            "strengths": [],
            "risks": [],
            "recommendation": "wait",
        }
        with patch(
            "gemini_analyzer.requests.post",
            return_value=_gemini_ok(raw),
        ) as mock_post:
            gemini_analyzer.analyze_game(self._game())

        called_url = mock_post.call_args.args[0]
        self.assertIn("generativelanguage.googleapis.com", called_url)

    def test_forced_gemini_ignores_groq_key(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        os.environ["AI_PROVIDER"] = "gemini"
        self.assertEqual(gemini_analyzer._active_provider(), "gemini")

    def test_forced_provider_without_key_degrades_to_fallback(self):
        os.environ["AI_PROVIDER"] = "groq"
        self.assertIsNone(gemini_analyzer._active_provider())

    def test_groq_failure_is_not_cached_and_reports_cause(self):
        """A Groq-side failure must stay out of the 24h cache (regression)."""
        os.environ["GROQ_API_KEY"] = "groq-key"
        with patch(
            "gemini_analyzer.requests.post",
            return_value=_JsonResponse(404, {"error": {"message": "model not found"}}),
        ):
            result = gemini_analyzer.analyze_game(self._game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_FALLBACK)
        joined = " ".join(result["risks"]) + result["recommendation"]
        self.assertIn("404", joined)
        self.assertIn("AI_MODEL", joined)
        self.assertIsNone(gemini_analyzer._cache_get(999))

    def test_groq_malformed_json_reports_cause(self):
        os.environ["GROQ_API_KEY"] = "groq-key"
        with patch(
            "gemini_analyzer.requests.post",
            return_value=_JsonResponse(
                200, {"choices": [{"message": {"content": "not json at all"}}]}
            ),
        ):
            result = gemini_analyzer.analyze_game(self._game())

        self.assertEqual(result["verdict"], gemini_analyzer.VERDICT_FALLBACK)
        self.assertIn("not valid JSON", " ".join(result["risks"]))


if __name__ == "__main__":
    unittest.main()
