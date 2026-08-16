import os
import tempfile
import unittest

import config

_db_path = os.path.join(tempfile.mkdtemp(), "portfolio-test.db")

import portfolio_db


class PortfolioDbTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(_db_path):
            os.remove(_db_path)
        portfolio_db.DATABASE = _db_path
        portfolio_db.create_portfolio_tables()

    def test_add_and_list_game(self):
        portfolio_db.add_portfolio_game(
            994732206,
            "HEAD TAP",
            role="QA Tester",
            description="QA testing across updates",
        )
        games = portfolio_db.list_portfolio_games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["game_id"], 994732206)
        self.assertEqual(games[0]["role"], "QA Tester")
        self.assertTrue(games[0]["visible"])

    def test_hidden_games_excluded_from_public_list(self):
        portfolio_db.add_portfolio_game(1, "A", visible=True)
        portfolio_db.add_portfolio_game(2, "B", visible=False)
        self.assertEqual([g["game_id"] for g in portfolio_db.list_portfolio_games()], [1])
        self.assertEqual(len(portfolio_db.list_portfolio_games(visible_only=False)), 2)

    def test_upsert_and_update(self):
        portfolio_db.add_portfolio_game(5, "Old", role="Dev")
        portfolio_db.add_portfolio_game(5, "New", role="QA")
        game = portfolio_db.get_portfolio_game(5)
        self.assertEqual(game["display_name"], "New")
        self.assertTrue(portfolio_db.update_portfolio_game(5, description="d1"))
        self.assertEqual(portfolio_db.get_portfolio_game(5)["description"], "d1")

    def test_snapshots_and_growth(self):
        portfolio_db.add_portfolio_game(10, "G", visible=True)
        portfolio_db.record_game_snapshot(10, 100, 1000, favorites=5, likes=90, dislikes=10)
        portfolio_db.record_game_snapshot(10, 200, 1500, favorites=7, likes=95, dislikes=10)
        latest = portfolio_db.get_latest_snapshot(10)
        self.assertEqual(latest["players"], 200)
        self.assertEqual(latest["like_ratio"], round(95 / (95 + 10) * 100, 2))
        growth = portfolio_db.compute_growth(10, 1)
        self.assertEqual(growth["players"], 100.0)
        self.assertEqual(growth["visits"], 50.0)
        self.assertEqual(portfolio_db.get_peak_players(10, hours=24), 200)

    def test_growth_needs_two_snapshots(self):
        portfolio_db.add_portfolio_game(11, "G")
        portfolio_db.record_game_snapshot(11, 100, 1000)
        growth = portfolio_db.compute_growth(11, 24)
        self.assertIsNone(growth["players"])
        self.assertIsNone(growth["visits"])

    def test_remove_wipes_snapshots(self):
        portfolio_db.add_portfolio_game(12, "G")
        portfolio_db.record_game_snapshot(12, 50, 500)
        portfolio_db.remove_portfolio_game(12)
        self.assertIsNone(portfolio_db.get_portfolio_game(12))
        self.assertEqual(portfolio_db.get_snapshots(12), [])

    def test_discord_snapshot(self):
        portfolio_db.add_portfolio_game(13, "G")
        portfolio_db.record_discord_snapshot(13, 12800)
        snap = portfolio_db.get_latest_discord_snapshot(13)
        self.assertEqual(snap["member_count"], 12800)


if __name__ == "__main__":
    unittest.main()
