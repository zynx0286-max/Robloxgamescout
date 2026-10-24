"""Every /settings option must actually filter.

Before this change only three of the ten stored values were read by any
filter, so these tests are the guard against a setting silently regressing
into a display-only decoration again.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filters import DEFAULT_MAX_AGE, base_thresholds, passes_alert_filters, thresholds_for


def _game(**overrides):
    """A game that passes every check with the default profile."""
    game = {
        "id": 1234,
        "place_id": 5678,
        "name": "Test Game",
        "playing": 100,
        "visits": 50_000,
        "favorites": 500,
        "growth": 10,
        "rating_percent": 88.0,
        "discord_invite": "https://discord.gg/abc123",
        "genre": "Simulator",
        "created": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "on_charts": True,
    }
    game.update(overrides)
    return game


def _settings(**overrides):
    """The users-table defaults, as get_user_filters() returns them."""
    settings = {
        "minimum_visits": 0,
        "maximum_visits": 1_500_000,
        "minimum_players": 15,
        "maximum_players": 2500,
        "minimum_growth": 0,
        "minimum_rating": 75,
        "require_discord": False,
        "require_charts": False,
        "genre": "Any",
        "max_age": DEFAULT_MAX_AGE,
    }
    settings.update(overrides)
    return settings


# --- baseline -------------------------------------------------------------

def test_baseline_game_passes():
    passed, failures = passes_alert_filters(_game(), _settings())
    assert passed, failures


def test_default_profile_does_not_change_base_thresholds():
    """The enforced user tier must be a no-op for an untouched profile.

    Every filter added here was previously ignored, so defaults that differ
    from the base tier would silently change what alerts.
    """
    assert thresholds_for(_settings()) == base_thresholds()


def test_no_settings_uses_base_thresholds():
    assert thresholds_for(None) == base_thresholds()


# --- CCU ------------------------------------------------------------------

def test_max_players_is_enforced():
    passed, failures = passes_alert_filters(_game(playing=9000), _settings())
    assert not passed
    assert any("above maximum" in f for f in failures)


def test_max_players_zero_means_no_cap():
    passed, failures = passes_alert_filters(
        _game(playing=9000), _settings(maximum_players=0)
    )
    assert passed, failures


def test_min_players_is_enforced():
    passed, failures = passes_alert_filters(_game(playing=5), _settings())
    assert not passed
    assert any("below minimum" in f for f in failures)


# --- visits ---------------------------------------------------------------

def test_max_visits_is_enforced():
    passed, failures = passes_alert_filters(
        _game(visits=2_000_000), _settings()
    )
    assert not passed
    assert any("at or above limit" in f for f in failures)


def test_max_visits_zero_means_no_cap():
    passed, failures = passes_alert_filters(
        _game(visits=2_000_000), _settings(maximum_visits=0)
    )
    assert passed, failures


def test_min_visits_is_enforced():
    passed, failures = passes_alert_filters(_game(visits=10), _settings(minimum_visits=1000))
    assert not passed
    assert any("below minimum" in f for f in failures)


# --- growth ---------------------------------------------------------------

def test_min_growth_is_enforced():
    passed, failures = passes_alert_filters(_game(growth=2), _settings(minimum_growth=25))
    assert not passed
    assert any("Growth 2.0% below minimum 25.0%" == f for f in failures)


# --- rating ---------------------------------------------------------------

def test_min_rating_is_enforced():
    passed, failures = passes_alert_filters(_game(rating_percent=80.0), _settings(minimum_rating=90))
    assert not passed
    assert any("below minimum 90.0%" in f for f in failures)


def test_missing_rating_is_rejected_while_a_floor_is_set():
    passed, failures = passes_alert_filters(_game(rating_percent=0), _settings())
    assert not passed
    assert "Rating data not available" in failures


def test_min_rating_zero_disables_the_check_entirely():
    passed, failures = passes_alert_filters(
        _game(rating_percent=0), _settings(minimum_rating=0)
    )
    assert passed, failures


# --- discord --------------------------------------------------------------

def test_default_profile_allows_a_missing_invite():
    """Discord is optional by default now.

    Roblox's social-link APIs (the only places an invite used to be readable)
    were withdrawn behind authentication and now return HTTP 401, so an invite
    can only be found in a game's description. Defaulting to "required" would
    demand something the bot usually cannot detect — and nothing would alert.
    """
    passed, failures = passes_alert_filters(_game(discord_invite=""), _settings())
    assert passed, failures


def test_require_discord_yes_rejects_a_missing_invite():
    passed, failures = passes_alert_filters(
        _game(discord_invite=""), _settings(require_discord=True)
    )
    assert not passed
    assert any("No Discord invite" in f for f in failures)


def test_without_discord_suspends_only_that_check():
    """The cheap first pass must differ from the real profile in one way only.

    Scans run the filters with the Discord requirement suspended so the
    two-requests-per-game invite lookup is only paid for on games that could
    actually alert; a game that passes the probe must still be judged by the
    full profile afterwards.
    """
    from filters import thresholds_for, without_discord

    settings = _settings(require_discord=True)
    probe = without_discord(settings)

    assert thresholds_for(probe)["require_discord"] is False
    for key in ("min_players", "max_players", "min_rating",
                "max_visits", "max_age", "require_charts"):
        assert thresholds_for(probe)[key] == thresholds_for(settings)[key]

    no_invite = _game(discord_invite="")
    assert passes_alert_filters(no_invite, probe)[0] is True
    assert passes_alert_filters(no_invite, settings)[0] is False


def test_without_discord_handles_a_missing_profile():
    from filters import thresholds_for, without_discord

    assert thresholds_for(without_discord(None))["require_discord"] is False
    # The input profile is not mutated.
    settings = _settings(require_discord=True)
    without_discord(settings)
    assert settings["require_discord"] is True


def test_malformed_discord_invite_is_rejected_when_required():
    passed, failures = passes_alert_filters(
        _game(discord_invite="https://example.com/not-discord"),
        _settings(require_discord=True),
    )
    assert not passed
    assert any("does not appear to be a valid invite URL" in f for f in failures)


# --- charts ---------------------------------------------------------------

def test_require_charts_rejects_games_off_the_charts():
    passed, failures = passes_alert_filters(
        _game(on_charts=False), _settings(require_charts=True)
    )
    assert not passed
    assert "Not on any Roblox chart this cycle" in failures


def test_require_charts_off_is_a_noop():
    passed, failures = passes_alert_filters(_game(on_charts=False), _settings())
    assert passed, failures


# --- genre ----------------------------------------------------------------

def test_genre_filter_is_case_insensitive_substring():
    passed, failures = passes_alert_filters(_game(genre="Simulator"), _settings(genre="sim"))
    assert passed, failures


def test_genre_filter_rejects_non_matches():
    passed, failures = passes_alert_filters(_game(genre="RPG"), _settings(genre="Simulator"))
    assert not passed
    assert any(f.startswith("Genre") for f in failures)


def test_genre_any_is_a_noop():
    passed, failures = passes_alert_filters(_game(genre=""), _settings(genre="Any"))
    assert passed, failures


# --- age ------------------------------------------------------------------

def test_max_age_is_enforced():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    passed, failures = passes_alert_filters(_game(created=old), _settings(max_age=365))
    assert not passed
    assert any(f.startswith("Age 4") for f in failures)


def test_max_age_ignores_games_with_an_unknown_date():
    passed, failures = passes_alert_filters(
        _game(created=None), _settings(max_age=1)
    )
    assert passed, failures


def test_max_age_zero_is_no_cap():
    old = (datetime.now(timezone.utc) - timedelta(days=4000)).isoformat()
    passed, failures = passes_alert_filters(_game(created=old), _settings())
    assert passed, failures


# --- reporting ------------------------------------------------------------

def test_every_failure_is_reported_at_once():
    passed, failures = passes_alert_filters(
        _game(playing=9000, rating_percent=10.0, discord_invite="", genre="RPG"),
        _settings(minimum_rating=90, genre="Simulator", require_discord=True),
    )
    assert not passed
    assert len(failures) >= 4


def test_rejection_attaches_roblox_link_and_market_links():
    game = _game()
    fails, _ = passes_alert_filters(game, _settings())
    assert fails
    assert game["roblox_url"].endswith("/5678")
    assert game["market_links"]


# --- schema migration -----------------------------------------------------

def test_upgrade_resets_the_never_enforced_max_age_and_adds_require_charts(
    tmp_path, monkeypatch
):
    """An existing profile must not suddenly be dropped for being too old.

    max_age defaulted to 365 while no code read it. Enforcing that stored
    default would silently filter out most of the catalogue, so the migration
    resets it to "no cap" and only an explicit choice filters.
    """
    import config
    import database

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            discord_id INTEGER PRIMARY KEY,
            minimum_visits INTEGER DEFAULT 0,
            maximum_visits INTEGER DEFAULT 1500000,
            minimum_players INTEGER DEFAULT 15,
            maximum_players INTEGER DEFAULT 2500,
            minimum_growth INTEGER DEFAULT 0,
            minimum_rating INTEGER DEFAULT 75,
            require_discord INTEGER DEFAULT 1,
            require_market_links INTEGER DEFAULT 1,
            genre TEXT DEFAULT 'Any',
            max_age INTEGER DEFAULT 365,
            alert_level TEXT DEFAULT 'all'
        )
        """
    )
    conn.execute("INSERT INTO users (discord_id) VALUES (777)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(database, "DATABASE", str(db_path))

    database.create_database()
    filters = database.get_user_filters(777)

    assert filters["max_age"] == 0
    assert filters["require_charts"] is False
    assert filters["minimum_rating"] == 75
    # The old "Yes" default is reset too — it was written when the social-link
    # APIs were still open, and it would now suppress every alert.
    assert filters["require_discord"] is False


def test_a_legacy_table_does_not_hand_new_users_its_old_defaults(tmp_path, monkeypatch):
    """SQLite freezes a column's DEFAULT at CREATE TABLE time.

    An existing database therefore keeps handing out require_discord=1 and
    max_age=365 to every new user even after the code's defaults change —
    and both of those now genuinely filter, so it would silently suppress
    every alert for anyone who ran /settings for the first time.
    """
    import config
    import database

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            discord_id INTEGER PRIMARY KEY,
            minimum_visits INTEGER DEFAULT 0,
            maximum_visits INTEGER DEFAULT 1500000,
            minimum_players INTEGER DEFAULT 15,
            maximum_players INTEGER DEFAULT 2500,
            minimum_growth INTEGER DEFAULT 0,
            minimum_rating INTEGER DEFAULT 75,
            require_discord INTEGER DEFAULT 1,
            require_market_links INTEGER DEFAULT 1,
            genre TEXT DEFAULT 'Any',
            max_age INTEGER DEFAULT 365,
            alert_level TEXT DEFAULT 'all'
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(database, "DATABASE", str(db_path))

    database.create_database()
    brand_new = database.get_user_filters(999)

    assert brand_new["require_discord"] is False
    assert brand_new["max_age"] == 0
    assert brand_new["require_charts"] is False


def test_missing_columns_are_added_without_rebuilding_the_table(tmp_path, monkeypatch):
    """A later-added setting must never cost a user their saved profile.

    _recreate_users_table() drops the table before recreating it, so only the
    original core columns may trigger a rebuild; everything added afterwards
    has to arrive via ALTER TABLE.
    """
    import config
    import database

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            discord_id INTEGER PRIMARY KEY,
            minimum_visits INTEGER DEFAULT 0,
            maximum_visits INTEGER DEFAULT 1500000,
            minimum_players INTEGER DEFAULT 15,
            maximum_players INTEGER DEFAULT 2500,
            minimum_growth INTEGER DEFAULT 0,
            minimum_rating INTEGER DEFAULT 75,
            require_discord INTEGER DEFAULT 1,
            alert_level TEXT DEFAULT 'all'
        )
        """
    )
    conn.execute("INSERT INTO users (discord_id, minimum_visits) VALUES (888, 4242)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(database, "DATABASE", str(db_path))

    database.create_database()
    filters = database.get_user_filters(888)

    # The stored value survived, and genre / max_age / require_charts were
    # added in place rather than by dropping the table.
    assert filters["minimum_visits"] == 4242
    assert filters["genre"] == "Any"
    assert filters["max_age"] == 0
    assert filters["require_charts"] is False
