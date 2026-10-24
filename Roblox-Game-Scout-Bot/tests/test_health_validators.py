"""Payload validators — catching \"HTTP 200 but no usable data\".

Roblox endpoints that answer 200 with an empty body used to pass the health
check while discovery quietly died. These validators are pure functions, so
each failure mode is tested without any network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from health_check import (
    validate_charts_payload,
    validate_games_payload,
    validate_omni_search_payload,
)


def test_games_payload_happy_path():
    ok, detail = validate_games_payload(
        {"data": [{"id": 994732206, "name": "Blox Fruits", "playing": 1}]}
    )

    assert ok is True
    assert detail == "Blox Fruits"


def test_games_payload_empty_data_is_a_warning_case():
    ok, detail = validate_games_payload({"data": []})

    assert ok is False
    assert "no game data" in detail


def test_games_payload_missing_data_key():
    ok, detail = validate_games_payload({})

    assert ok is False


def test_games_payload_shape_change_is_flagged():
    """The API renamed fields: 200 with junk must not read as healthy."""
    ok, detail = validate_games_payload({"data": [{"universeId": 1}]})

    assert ok is False
    assert "shape changed" in detail


def test_games_payload_none_is_survivable():
    ok, _ = validate_games_payload(None)

    assert ok is False


def test_omni_search_payload_counts_contents():
    ok, detail = validate_omni_search_payload({
        "searchResults": [
            {"contents": [{"gameId": 1}, {"gameId": 2}]},
            {"contents": [{"gameId": 3}]},
        ]
    })

    assert ok is True
    assert "3 results" in detail


def test_omni_search_empty_results_flagged():
    ok, detail = validate_omni_search_payload({"searchResults": []})

    assert ok is False
    assert "returned 0 results" in detail


def test_omni_search_missing_key():
    ok, _ = validate_omni_search_payload({})

    assert ok is False


def test_charts_payload_happy_path():
    ok, detail = validate_charts_payload({
        "games": [{"universeId": 1, "name": "Charted"}]
    })

    assert ok is True
    assert "1 games on sort" in detail


def test_charts_payload_empty_games_flagged():
    ok, detail = validate_charts_payload({"games": []})

    assert ok is False
    assert "no games" in detail


def test_charts_payload_shape_change_is_flagged():
    ok, detail = validate_charts_payload({"games": [{"id": None}]})

    assert ok is False
    assert "shape changed" in detail


def test_charts_payload_root_place_id_only_is_rejected():
    """A universe id is what discovery keys on; rootPlaceId alone is junk."""
    ok, detail = validate_charts_payload({
        "games": [{"rootPlaceId": 42, "name": "X"}]
    })

    assert ok is False
    assert "universeId" in detail
