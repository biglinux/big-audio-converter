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
