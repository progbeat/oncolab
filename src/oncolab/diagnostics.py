"""Human-facing outcomes and safe diagnostic locations."""
import os
import sys
import re


def outcome(label, message, *, color=None, terminal=None):
    if color is None:
        color = not os.environ.get("NO_COLOR")
    heading = f"[{label}]"
    if terminal is None:
        terminal = sys.stderr.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    if color and terminal:
        heading = f"\x1b[31m{heading}\x1b[0m"
    return f"{heading} {message}"


def safe_subprocess_details(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    for filename, number, function in re.findall(
            r'^\s*File "[^"\n]*/([A-Za-z0-9_.-]+)", line (\d+), in ([A-Za-z0-9_<>.]+)[ \t]*$',
            text, re.MULTILINE):
        yield f"  {filename}:{number} in {function}"
    for filename, number, function in re.findall(
            r"(?m)^│[ \t]*(?:[^│\n]*/)?([A-Za-z0-9_.-]+):([0-9]+) in ([A-Za-z0-9_<>.]+)[ \t]*│$", text):
        yield f"  {filename}:{number} in {function}"
    for kind in re.findall(r"(?m)^\s*(\w+(?:Error|Exception)):", text):
        yield f"  {kind}"
    for marker, summary in (
        ("can't open file", "Python could not open the worker file"),
        ("No module named", "a Python module could not be imported"),
        ("Fatal Python error", "Python reported a fatal startup error"),
    ):
        if marker in text:
            yield f"  {summary}"
