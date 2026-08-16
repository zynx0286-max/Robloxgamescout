import os
import tempfile
import unittest

import config

_db_path = os.path.join(tempfile.mkdtemp(), "portfolio-api-test.db")
config.PORTFOLIO_CACHE_SECONDS = 0  # disable caching so tests see fresh data

import portfolio_db
import portfolio_api

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi TestClient (httpx) not installed")
class PortfolioApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portfolio_db.DATABASE = _db_path
        portfolio_db.create_portfolio_tables()
        portfolio_db.add_portfolio_game(
            994732206,
            "HEAD TAP",
            role="QA Tester",
            description="QA testing across updates",
            roblox_url="https://www.roblox.com/games/994732206",
        )
        portfolio_db.add_portfolio_game(999999, "Hidden Game", visible=False)
        portfolio_db.record_game_snapshot(994732206, 184, 1240000, favorites=8430, likes=115000, dislikes=7100)
        portfolio_db.record_game_snapshot(994732206, 200, 1250000, favorites=8500, likes=116000, dislikes=7100)
        cls.client = TestClient(portfolio_api.app)

    def test_health(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertGreaterEqual(body["projects"], 1)

    def test_portfolio_list_excludes_hidden(self):
        r = self.client.get("/api/v1/portfolio")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["projects"]), 1)
        project = body["projects"][0]
        self.assertEqual(project["name"], "HEAD TAP")
        self.assertEqual(project["stats"]["players"], 200)
        self.assertEqual(
            project["stats"]["growth"]["players_1h"],
            round((200 - 184) / 184 * 100, 2),
        )

    def test_project_detail(self):
        r = self.client.get("/api/v1/portfolio/994732206")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["role"], "QA Tester")
        self.assertEqual(body["stats"]["like_ratio"], round(116000 / (116000 + 7100) * 100, 2))

    def test_project_not_found(self):
        self.assertEqual(self.client.get("/api/v1/portfolio/4242424242").status_code, 404)
        # hidden games are 404 for the public API too
        self.assertEqual(self.client.get("/api/v1/portfolio/999999").status_code, 404)

    def test_stats_endpoint(self):
        r = self.client.get("/api/v1/portfolio/994732206/stats")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"]["visits"], 1250000)

    def test_history(self):
        r = self.client.get("/api/v1/portfolio/994732206/history?period=7d")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["points"]), 2)
        self.assertEqual(body["points"][0]["players"], 184)


if __name__ == "__main__":
    unittest.main()
