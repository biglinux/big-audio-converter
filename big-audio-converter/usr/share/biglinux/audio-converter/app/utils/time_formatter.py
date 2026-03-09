# app/utils/time_formatter.py

"""
Utility functions for formatting time values.
"""

def format_time_short(seconds):
    """Format time for timeline display (compact format with hundredths)."""
    # Round to 2 decimal places for hundredths precision
    seconds = round(seconds, 2)

    if seconds >= 3600:
        # Hours:Minutes:Seconds.Hundredths for long files
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"
    else:
        # Minutes:Seconds.Hundredths for shorter files
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}:{secs:05.2f}"


def format_time_display(seconds):
    """Format seconds to MM:SS or H:MM:SS for simple display (no decimals)."""
    if seconds is None or seconds < 0:
        return "0:00"
    seconds = round(seconds, 1)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_time_ruler(seconds, mark_interval):
    """Format time for ruler marks with interval-appropriate precision."""
    if seconds is None or seconds < 0:
        seconds = 0
    if mark_interval < 1:
        return format_time_short(seconds)
    else:
        return format_time_display(seconds)
