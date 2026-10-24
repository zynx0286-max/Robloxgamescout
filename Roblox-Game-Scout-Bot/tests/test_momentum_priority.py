"""Tests for the momentum-aware discovery priority classifier.

_disclassify scan priority used to look at Scout Score + growth only; a
game with a fresh velocity spike or a 5+ chart-position climb now promotes
one tier, and a declining 72h trend demotes one. All inputs come from data
the pipeline already computes, so the signals are free.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler


def _game(score=0, growth=0, velocity=None, chart_delta=None):
    game = {"scout_score": {"total": score}, "growth": growth}
    if velocity is not None:
        game["velocity"] = velocity
    if chart_delta is not None:
        game["chart_rank_delta"] = chart_delta
    return game


def test_base_tiers_are_unchanged_without_momentum_signals():
    assert scheduler._classify_scan_priority(_game(score=90)) == scheduler.PRIORITY_HIGH
    assert scheduler._classify_scan_priority(_game(growth=100)) == scheduler.PRIORITY_HIGH
    assert scheduler._classify_scan_priority(_game(score=70)) == scheduler.PRIORITY_MEDIUM
    assert scheduler._classify_scan_priority(_game(growth=30)) == scheduler.PRIORITY_MEDIUM
    assert scheduler._classify_scan_priority(_game(score=10, growth=5)) == scheduler.PRIORITY_LOW


def test_velocity_spike_promotes_low_to_medium():
    game = _game(score=10, growth=5,
                 velocity={"ccu_spike": 2.5, "ccu_trend": "spiking"})
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_MEDIUM


def test_velocity_spike_promotes_medium_to_high():
    game = _game(score=75, growth=30,
                 velocity={"ccu_spike": 3.0, "ccu_trend": "spiking"})
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_HIGH


def test_chart_climb_promotes_low_to_medium():
    game = _game(score=10, growth=5, chart_delta=8)
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_MEDIUM


def test_small_climb_does_not_promote():
    game = _game(score=10, growth=5, chart_delta=3)
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_LOW


def test_declining_trend_demotes_medium_to_low():
    game = _game(score=75, growth=30,
                 velocity={"ccu_spike": 0.4, "ccu_trend": "declining"})
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_LOW


def test_declining_trend_demotes_high_to_medium():
    game = _game(score=90, growth=120,
                 velocity={"ccu_spike": 0.3, "ccu_trend": "declining"})
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_MEDIUM


def test_high_never_promotes_or_demotes_below_medium():
    """A spiking high stays high; a declining high lands on medium, not low."""
    spiking = _game(score=90, growth=120,
                    velocity={"ccu_spike": 3.0, "ccu_trend": "spiking"})
    assert scheduler._classify_scan_priority(spiking) == scheduler.PRIORITY_HIGH


def test_garbage_velocity_and_delta_values_do_not_crash():
    game = _game(score=10, growth=5,
                 velocity={"ccu_spike": "not-a-number", "ccu_trend": "spiking"},
                 chart_delta="junk")
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_LOW


def test_missing_velocity_dict_is_treated_as_neutral():
    game = _game(score=10, growth=5)
    game["velocity"] = None
    assert scheduler._classify_scan_priority(game) == scheduler.PRIORITY_LOW
