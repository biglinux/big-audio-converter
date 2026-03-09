"""Unit tests for BAC time_formatter module."""

import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "big-audio-converter",
        "usr",
        "share",
        "biglinux",
        "audio-converter",
    ),
)

from app.utils.time_formatter import format_time_display, format_time_ruler, format_time_short


class TestFormatTimeShort:
    def test_zero(self):
        assert format_time_short(0) == "0:00.00"

    def test_one_second(self):
        assert format_time_short(1.0) == "0:01.00"

    def test_with_centiseconds(self):
        assert format_time_short(1.23) == "0:01.23"

    def test_one_minute(self):
        assert format_time_short(60.0) == "1:00.00"

    def test_mixed(self):
        assert format_time_short(125.5) == "2:05.50"

    def test_over_one_hour(self):
        result = format_time_short(3661.5)
        assert result == "1:01:01.50"

    def test_exactly_one_hour(self):
        result = format_time_short(3600.0)
        assert result == "1:00:00.00"


class TestFormatTimeDisplay:
    def test_zero(self):
        assert format_time_display(0) == "0:00"

    def test_none(self):
        assert format_time_display(None) == "0:00"

    def test_negative(self):
        assert format_time_display(-5) == "0:00"

    def test_seconds_only(self):
        assert format_time_display(45) == "0:45"

    def test_minutes_seconds(self):
        assert format_time_display(125) == "2:05"

    def test_with_hours(self):
        assert format_time_display(3661) == "1:01:01"

    def test_rounds(self):
        # 59.9 rounds to 59.9, int(59.9 % 60) = 59
        assert format_time_display(59.9) == "0:59"


class TestFormatTimeRuler:
    def test_sub_second_interval_uses_short(self):
        result = format_time_ruler(1.23, 0.5)
        assert "." in result

    def test_one_second_interval_uses_display(self):
        result = format_time_ruler(60, 1)
        assert result == "1:00"

    def test_none_seconds(self):
        result = format_time_ruler(None, 1)
        assert result == "0:00"

    def test_negative_seconds(self):
        result = format_time_ruler(-5, 1)
        assert result == "0:00"
