import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import database


class SettingsPersistenceTests(unittest.TestCase):
    """User filter choices must survive get_user_filters() reads.

    Regression: the old get_user_filters() force-migrated
    minimum_rating/require_discord back to defaults on every read, which
    silently reverted anything a user changed via /settings and made those
    settings read-only decorations.
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        database.DATABASE = self.temp_db.name
        database.create_database()

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_update_user_persists_all_documented_fields(self):
        database.update_user(
            1,
            minimum_rating=60,
            require_discord=False,
            require_charts=True,
        )

        filters = database.get_user_filters(1)
        self.assertEqual(filters["minimum_rating"], 60)
        self.assertIs(filters["require_discord"], False)
        self.assertIs(filters["require_charts"], True)

    def test_relaxed_settings_survive_repeated_reads(self):
        database.update_user(2, minimum_rating=0, require_discord=False)

        for _ in range(3):
            filters = database.get_user_filters(2)
            self.assertEqual(filters["minimum_rating"], 0)
            self.assertIs(filters["require_discord"], False)

    def test_partial_update_only_touches_given_fields(self):
        database.update_user(3, require_discord=False, minimum_rating=50)
        database.update_user(3, minimum_rating=80)  # no require_discord arg

        filters = database.get_user_filters(3)
        self.assertEqual(filters["minimum_rating"], 80)
        self.assertIs(filters["require_discord"], False)

    def test_other_settings_still_normalize_on_read(self):
        # The legacy broad defaults should still migrate to the acquisition
        # defaults — that behavior was intentional and is kept.
        database.update_user(4, minimum_visits=100000, maximum_visits=0)

        filters = database.get_user_filters(4)
        self.assertEqual(filters["minimum_visits"], 0)
        self.assertEqual(filters["maximum_visits"], 1_500_000)


if __name__ == "__main__":
    unittest.main()
