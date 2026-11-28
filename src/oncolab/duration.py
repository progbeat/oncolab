"""Compact durations for human-facing progress and diagnostics."""


def format_duration(seconds):
    seconds = max(0.0, seconds)
    for scale, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= scale:
            return f"{seconds / scale:.1f}{unit}"
    return f"{seconds:.1f}s"
