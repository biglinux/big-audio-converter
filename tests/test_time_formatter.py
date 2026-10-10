"""Times on the seek bar, the queue and the waveform ruler."""

import pytest
from app.ui.time_formatter import (
    format_time_display,
    format_time_ruler,
    format_time_short,
)


@pytest.mark.parametrize(
    ("format_time", "seconds", "expected"),
    [
        (format_time_short, 125.5, "2:05.50"),
        (format_time_short, 3661.5, "1:01:01.50"),
        (format_time_display, None, "0:00"),
        (format_time_display, -5, "0:00"),
        (format_time_display, 59.9, "0:59"),
        (format_time_display, 3661, "1:01:01"),
    ],
)
def test_times_read_as_minutes_and_hours(format_time, seconds, expected):
    assert format_time(seconds) == expected


def test_ruler_shows_fractions_only_below_a_second():
    assert "." in format_time_ruler(1.23, 0.5)
    assert format_time_ruler(60, 1) == "1:00"
