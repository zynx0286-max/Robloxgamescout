"""Tests for Scout Score 2.0's velocity component.

The Velocity breakdown row is budgeted 0-20, but it used to append the raw
0-100 value from velocity.get_trending_score_from_velocity() — totals of
fast-growing games overflowed their component caps and the final 100-point
clamp then flattened every such game to 100, destroying the ranking order
between strong candidates.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner


def _strong_velocity() -> dict:
    """A velocity dict that earns the maximum 100 from the indexer."""
    return {
        "ccu_spike": 3.5,
        "ccu_trend": "spiking",
        "growth_consistency": 1.0,
        "visit_acceleration": 200000,
        "snapshot_count": 10,
        "avg_players": 100.0,
        "current_players": 350,
    }


def test_velocity_component_is_capped_inside_the_breakdown():
    game = {"playing": 5000, "visits": 5_000_000, "favorites": 100_000,
            "growth": 120, "velocity": _strong_velocity(),
            "created": "2024-01-01T00:00:00Z"}

    result = asyncio.run(scanner.calculate_scout_score(game))

    velocity_row = next(pts for label, pts, _ in result["breakdown"]
                        if label.startswith("🚀 Velocity"))
    assert velocity_row <= 20, (
        f"Velocity component contributed {velocity_row}; its budget is 0-20"
    )


def test_maxed_velocity_no_longer_forces_a_flat_100_total():
    """The old bug's exact symptom: every strong game scored 100."""
    game = {"playing": 10000, "visits": 5_000_000, "favorites": 100_000,
            "growth": 120, "velocity": _strong_velocity(),
            "created": "2024-01-01T00:00:00Z"}

    result = asyncio.run(scanner.calculate_scout_score(game))

    # Pin each component's budget so the scaling math itself is locked in:
    # growth 30 + momentum 25 + velocity 20 saturates this fixture; under
    # the old bug the raw 0-100 velocity value overflowed and produced a
    # flat 100 regardless of the rest.
    named = {label: pts for label, pts, _ in result["breakdown"]}
    assert named["📈 Growth"] == 30
    assert named["👥 Player momentum"] == 25
    assert named["🚀 Velocity"] == 20
    assert result["total"] <= 100


def test_total_is_not_flat_100_for_a_halfway_game():
    """A game with strong growth but mid-range momentum must NOT hit 100."""
    game = {"playing": 1200, "visits": 500_000, "favorites": 5_000,
            "growth": 120, "velocity": _strong_velocity(),
            "created": "2024-01-01T00:00:00Z"}

    result = asyncio.run(scanner.calculate_scout_score(game))

    named = {label: pts for label, pts, _ in result["breakdown"]}
    assert named["📈 Growth"] == 30
    assert named["👥 Player momentum"] == 15
    assert named["🚀 Velocity"] == 20
    # Under the old overflow bug this fixture scored the same flat 100 as
    # the stadium-scale game; the honest component sum stays below it.
    assert result["total"] < 100


def test_insufficient_history_still_scores_zero():
    game = {"playing": 500, "visits": 10_000, "favorites": 10,
            "growth": 5, "velocity": {}, "created": None}

    result = asyncio.run(scanner.calculate_scout_score(game))

    velocity_row = next(pts for label, pts, text in result["breakdown"]
                        if label.startswith("🚀 Velocity"))
    assert velocity_row == 0
    assert "Insufficient history" in velocity_text(result)


def velocity_text(result):
    return next(text for label, _, text in result["breakdown"]
                if label.startswith("🚀 Velocity"))


def test_scaling_preserves_ordering_between_weak_and_strong():
    weak_game = {"playing": 500, "visits": 10_000, "favorites": 10,
                 "growth": 5,
                 "velocity": {"ccu_spike": 1.1, "ccu_trend": "stable",
                              "growth_consistency": 0.2,
                              "visit_acceleration": 0,
                              "snapshot_count": 5},
                 "created": "2024-01-01T00:00:00Z"}
    strong_game = {"playing": 500, "visits": 10_000, "favorites": 10,
                   "growth": 5,
                   "velocity": _strong_velocity(),
                   "created": "2024-01-01T00:00:00Z"}

    weak = asyncio.run(scanner.calculate_scout_score(weak_game))
    strong = asyncio.run(scanner.calculate_scout_score(strong_game))

    assert strong["total"] > weak["total"]
