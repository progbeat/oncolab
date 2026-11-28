"""Compact durations for human-facing progress and diagnostics."""


def format_duration(seconds, *, round_up=False):
    """Round to one decimal, optionally never understating the time left."""
    seconds = max(0.0, seconds)
    units = ((1, "s"), (60, "m"), (3600, "h"), (86400, "d"))
    index = len(units) - 1
    while index and seconds < units[index][0]:
        index -= 1
    scale, unit = units[index]
    tenths = round(seconds * 10 / scale)
    if round_up and tenths * scale / 10 < seconds:
        tenths += 1
    # Carry at the display boundary instead of showing 60.0s/60.0m/24.0h.
    if index + 1 < len(units) and tenths * scale >= 10 * units[index + 1][0]:
        return f"1.0{units[index + 1][1]}"
    return f"{tenths / 10:.1f}{unit}"
