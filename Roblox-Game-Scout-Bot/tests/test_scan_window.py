"""Scheduled-scan time-window tests (default: 10 AM–9 PM ET, every day)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import scheduler

ET = ZoneInfo("America/New_York")


def _set_window(active=True, start=10, end=21):
    scheduler.SCAN_ACTIVE_HOURS = active
    scheduler.SCAN_START_HOUR_ET = start
    scheduler.SCAN_END_HOUR_ET = end
    scheduler._ET = ET


def test_inside_window_scans():
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 12, 0, tzinfo=ET)
    ) is True


def test_first_minute_of_window_scans():
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 10, 0, tzinfo=ET)
    ) is True


def test_before_window_does_not_scan():
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 9, 59, tzinfo=ET)
    ) is False


def test_at_end_hour_does_not_scan():
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 21, 0, tzinfo=ET)
    ) is False


def test_overnight_does_not_scan():
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 3, 0, tzinfo=ET)
    ) is False


def test_disabled_window_scans_247():
    _set_window(active=False)
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 3, 0, tzinfo=ET)
    ) is True


def test_degenerate_window_scans_247():
    _set_window(start=21, end=10)
    assert scheduler._within_active_window(
        datetime(2026, 9, 26, 3, 0, tzinfo=ET)
    ) is True


def test_utc_input_converted_to_et_winter():
    # January: EST = UTC-5. 15:00 UTC == 10:00 EST -> inside the window.
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    ) is True
    # 14:59 UTC == 9:59 EST -> still before the window opens.
    assert scheduler._within_active_window(
        datetime(2026, 1, 15, 14, 59, tzinfo=timezone.utc)
    ) is False


def test_utc_input_converted_to_et_summer():
    # July: EDT = UTC-4. 14:00 UTC == 10:00 EDT -> inside the window.
    _set_window()
    assert scheduler._within_active_window(
        datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    ) is True
    assert scheduler._within_active_window(
        datetime(2026, 7, 15, 13, 59, tzinfo=timezone.utc)
    ) is False


def test_naive_datetime_does_not_raise():
    # A naive datetime falls back to server-local assumptions; just verify
    # the function tolerates it and returns a bool.
    _set_window()
    result = scheduler._within_active_window(datetime(2026, 9, 26, 12, 0))
    assert isinstance(result, bool)
