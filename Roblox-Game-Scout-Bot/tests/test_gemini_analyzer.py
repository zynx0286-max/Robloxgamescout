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
        gemini_analyzer._ensure_table()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
