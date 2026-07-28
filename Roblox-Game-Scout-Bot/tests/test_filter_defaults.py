import sys
from pathlib import Path


def test_default_user_filters_match_acquisition_requirements(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import config
    import database

    db_path = tmp_path / "scoutbot-test.db"
    original_database_path = database.DATABASE
    original_config_path = config.DATABASE_PATH

    try:
        config.DATABASE_PATH = str(db_path)
        database.DATABASE = str(db_path)
        database.create_database()

        user_filters = database.get_user_filters(424242)

        assert user_filters["minimum_visits"] == 0
        assert user_filters["maximum_visits"] == 1_500_000
        assert user_filters["minimum_players"] == 15
        assert user_filters["maximum_players"] == 2500
        assert user_filters["minimum_growth"] == 0
    finally:
        config.DATABASE_PATH = original_config_path
        database.DATABASE = original_database_path
