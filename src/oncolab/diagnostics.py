"""Extract diagnostic locations from subprocess output without replaying its data."""
import re


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
