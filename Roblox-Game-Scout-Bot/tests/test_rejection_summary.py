"""Rejection analytics — the answer to \"why did the scan come back empty?\".

Two historic bugs both produced the same symptom: a scan that returned
nothing while looking perfectly healthy (ratings fetched after the filters;
chart flags dropped between discovery and the filters). A dominant-reason
summary makes that class of bug legible on day one: "0 of 257 games
rejected — top reasons: Rating (257)" points straight at the culprit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filters import format_rejection_summary, summarize_rejections


def test_reasons_bucket_into_stable_categories():
    rejections = [
        ["CCU 5 below minimum 15"],
        ["CCU 2 below minimum 15"],
        ["Visits 2,000,000 at or above limit 1,500,000"],
        ["Rating data not available"],
    ]

    tally = summarize_rejections(rejections)

    assert tally == [("CCU window", 2), ("Rating", 1), ("Visits window", 1)]


def test_tally_is_sorted_by_count_descending():
    rejections = [
        ["Rating data not available"],
        ["Rating data not available"],
        ["Rating data not available"],
        ["CCU 5 below minimum 15"],
    ]

    tally = summarize_rejections(rejections)

    assert tally[0] == ("Rating", 3)
    assert tally[1] == ("CCU window", 1)


def test_every_reason_of_a_rejected_game_counts():
    """evaluate() returns ALL failures at once — each one must be tallied."""
    rejections = [
        [
            "CCU 5 below minimum 15",
            "Rating data not available",
            "Not on any Roblox chart this cycle",
        ]
    ]

    tally = summarize_rejections(rejections)

    assert dict(tally) == {
        "CCU window": 1,
        "Rating": 1,
        "Chart presence": 1,
    }


def test_discord_and_age_and_genre_and_growth_categories():
    rejections = [
        ["No Discord invite found via social links, description, or group"],
        ["Discord link 'not-a-link' does not appear to be a valid invite URL"],
        ["Age 400d exceeds maximum 30d"],
        ["Genre 'Puzzle' does not match 'Simulator'"],
        ["Growth 1.0% below minimum 10.0%"],
    ]

    tally = dict(summarize_rejections(rejections))

    assert tally["Discord invite"] == 2
    assert tally["Age"] == 1
    assert tally["Genre"] == 1
    assert tally["Growth"] == 1


def test_unknown_reasons_land_in_other():
    tally = summarize_rejections([["Something entirely new went wrong"]])

    assert tally == [("Other", 1)]


def test_empty_input_is_an_empty_tally():
    assert summarize_rejections([]) == []
    assert summarize_rejections(None) == []
    assert summarize_rejections([[]]) == []


def test_format_names_the_dominant_reason():
    rejections = [["Rating data not available"] for _ in range(257)]

    note = format_rejection_summary(rejections, collected=257)

    assert "257 of 257 games rejected" in note
    assert "Rating (257)" in note


def test_format_lists_up_to_three_reasons():
    rejections = (
        [["Rating data not available"]] * 10
        + [["CCU 5 below minimum 15"]] * 5
        + [["Age 400d exceeds maximum 30d"]] * 2
        + [["Genre 'Puzzle' does not match 'Simulator'"]]
    )

    note = format_rejection_summary(rejections)

    assert "Rating (10)" in note
    assert "CCU window (5)" in note
    assert "Age (2)" in note
    assert "Genre" not in note  # only the top 3 make the line


def test_format_is_empty_when_there_is_nothing_to_report():
    assert format_rejection_summary([]) == ""
    assert format_rejection_summary(None) == ""
