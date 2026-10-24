"""A wrong token must produce advice, not a raw traceback.

Discord rejects an invalid token with the opaque
``LoginFailure: Improper token has been passed``. The startup path must
(1) clean the whitespace/quotes/``Bot `` prefix that hosting-panel input
fields keep, (2) warn when the value cannot be a bot token at all, and
(3) still exit non-zero when the token is missing entirely.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as main_module

# Shaped like a real token: three dot-separated segments of the allowed
# alphabet, each long enough to satisfy the heuristic.
WELL_FORMED_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 27


def test_token_cleaning_strips_panel_noise(monkeypatch):
    monkeypatch.setenv(
        "DISCORD_TOKEN", '  Bot "MTIzNDU2Nzg5MDEyMzQ1Njc4.abc123.dGVzdA" \n'
    )
    token, cleaned = main_module._clean_env_str(
        "DISCORD_TOKEN", strip_bot_prefix=True
    )
    assert token == "MTIzNDU2Nzg5MDEyMzQ1Njc4.abc123.dGVzdA"
    assert cleaned is True


def test_clean_value_is_reported_as_unchanged(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", WELL_FORMED_TOKEN)
    token, cleaned = main_module._clean_env_str(
        "DISCORD_TOKEN", strip_bot_prefix=True
    )
    assert token == WELL_FORMED_TOKEN
    assert cleaned is False


def test_missing_variable_returns_none(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    assert main_module._clean_env_str(
        "DISCORD_TOKEN", strip_bot_prefix=True
    ) == (None, False)


def test_guild_id_is_cleaned_too(monkeypatch):
    monkeypatch.setenv("GUILD_ID", ' "123456789012345678" ')
    guild_id, cleaned = main_module._clean_env_str("GUILD_ID")
    assert guild_id == "123456789012345678"
    assert cleaned is True


def _silence_optional_warnings(monkeypatch):
    """Set every variable _check_startup_env warns about except the token."""
    monkeypatch.setattr(main_module, "GUILD_ID", "123456789012345678")
    monkeypatch.setattr(main_module, "GUILD_ID_NEEDED_CLEANING", False)
    monkeypatch.setenv("ALERT_CHANNEL_ID", "987654321098765432")


def test_missing_token_exits_with_fatal_message(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "TOKEN", None)
    monkeypatch.setattr(main_module, "TOKEN_NEEDED_CLEANING", False)
    _silence_optional_warnings(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        main_module._check_startup_env()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "DISCORD_TOKEN is not set" in out
    assert "Startup tab" in out  # points at the host panel, not the code


def test_unrecognizable_token_warns_but_does_not_exit(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "TOKEN", "your_token_here")
    monkeypatch.setattr(main_module, "TOKEN_NEEDED_CLEANING", False)
    _silence_optional_warnings(monkeypatch)

    main_module._check_startup_env()  # must not raise

    out = capsys.readouterr().out
    assert "does not look like a bot token" in out
    assert "Reset Token" in out  # names the likely mix-up


def test_well_formed_token_gets_no_token_warnings(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "TOKEN", WELL_FORMED_TOKEN)
    monkeypatch.setattr(main_module, "TOKEN_NEEDED_CLEANING", False)
    _silence_optional_warnings(monkeypatch)

    main_module._check_startup_env()  # must not raise

    out = capsys.readouterr().out
    assert "does not look like a bot token" not in out
    assert "DISCORD_TOKEN is not set" not in out


def test_cleaned_token_announces_the_cleanup(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "TOKEN", WELL_FORMED_TOKEN)
    monkeypatch.setattr(main_module, "TOKEN_NEEDED_CLEANING", True)
    _silence_optional_warnings(monkeypatch)

    main_module._check_startup_env()

    out = capsys.readouterr().out
    assert "cleaned automatically" in out
