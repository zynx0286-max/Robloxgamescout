"""Alert buttons: DynamicItem templates keep presses working after restarts.

The old design registered one persistent view with bare custom_ids
(``alert_claim``) while alert embeds shipped suffixed ids
(``alert_claim:<game_id>``). discord.py matches persistent views by exact
custom_id, so every pre-restart press failed silently. These tests pin the
replacement contract:

  * every button's custom_id carries the game id;
  * each class template matches exactly that shape (fullmatch);
  * ``from_custom_id`` reconstructs the game id;
  * the callbacks perform their database action on a real (temp) database.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import database


def _db(tmp_path, monkeypatch) -> str:
    import chart_rank
    import history

    db_path = str(tmp_path / "buttons.db")
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(database, "DATABASE", db_path)
    monkeypatch.setattr(history, "DATABASE_PATH", db_path)
    monkeypatch.setattr(chart_rank, "DATABASE_PATH", db_path)
    database.create_database()
    return db_path


GAME = {"id": 4242, "name": "Button Game", "playing": 100, "visits": 1000}


def _fake_interaction(user_id=123):
    sent = []

    async def defer(ephemeral=False):
        sent.append(("defer", ephemeral))

    class _Followup:
        async def send(self, content, ephemeral=False):
            sent.append(("send", content))

    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id),
        response=types.SimpleNamespace(defer=defer),
        followup=_Followup(),
        _sent=sent,
    )


def test_every_button_custom_id_carries_the_game_id():
    from buttons import AlertButtons

    view = AlertButtons.with_game(GAME)
    ids = [item.custom_id for item in view.children]
    assert ids == [
        "alert_claim:4242",
        "alert_accept:4242",
        "alert_reject:4242",
        "alert_more_info:4242",
    ]


def test_templates_match_the_suffixed_custom_id_and_nothing_else():
    from buttons import ALL_ALERT_BUTTONS

    for cls in ALL_ALERT_BUTTONS:
        pattern = cls.__discord_ui_compiled_template__
        assert pattern.fullmatch(f"{cls.ACTION}:12345"), cls.ACTION
        assert pattern.fullmatch(f"{cls.ACTION}:994732206"), cls.ACTION
        # The bare id (what the old persistent view registered) must NOT
        # match — that ambiguity is exactly what killed post-restart presses.
        assert pattern.fullmatch(cls.ACTION) is None, cls.ACTION
        assert pattern.fullmatch(f"{cls.ACTION}:abc") is None, cls.ACTION


def test_from_custom_id_reconstructs_the_game_id():
    import re
    from buttons import ALL_ALERT_BUTTONS

    for cls in ALL_ALERT_BUTTONS:
        match = cls.__discord_ui_compiled_template__.fullmatch(
            f"{cls.ACTION}:994732206"
        )
        item = asyncio.run(cls.from_custom_id(None, None, match))
        assert item.game_id == 994732206
        # No in-memory game: the callback must resolve it from Roblox.
        assert item.game is None


def test_claim_saves_but_does_not_watch(tmp_path, monkeypatch):
    from buttons import ClaimButton

    _db(tmp_path, monkeypatch)
    interaction = _fake_interaction(user_id=777)
    asyncio.run(ClaimButton(4242, GAME).callback(interaction))

    saved = database.get_saved_games_for_user(777)  # rows of (game_id, name, date)
    assert [g[0] for g in saved] == [4242]
    assert database.is_user_watching(777, 4242) is False
    assert any("Claimed" in content for kind, content in interaction._sent if kind == "send")


def test_accept_saves_and_watches(tmp_path, monkeypatch):
    from buttons import AcceptButton

    _db(tmp_path, monkeypatch)
    interaction = _fake_interaction(user_id=888)
    asyncio.run(AcceptButton(4242, GAME).callback(interaction))

    assert [g[0] for g in database.get_saved_games_for_user(888)] == [4242]
    assert database.is_user_watching(888, 4242) is True
    assert any("watchlist" in content for kind, content in interaction._sent if kind == "send")


def test_reject_ignores_for_that_user_only(tmp_path, monkeypatch):
    from buttons import RejectButton

    _db(tmp_path, monkeypatch)
    asyncio.run(RejectButton(4242, GAME).callback(_fake_interaction(user_id=999)))

    assert database.is_user_ignoring(999, 4242) is True
    assert database.is_user_ignoring(555, 4242) is False


def test_unknown_game_reports_cleanly_instead_of_raising(tmp_path, monkeypatch):
    import buttons
    from buttons import ClaimButton

    _db(tmp_path, monkeypatch)

    async def fetch_none(game_id):
        return None

    monkeypatch.setattr(buttons, "_fetch_game", fetch_none)
    interaction = _fake_interaction(user_id=111)
    asyncio.run(ClaimButton(4242, None).callback(interaction))

    assert any(
        "Could not identify the game" in content
        for kind, content in interaction._sent
        if kind == "send"
    )
