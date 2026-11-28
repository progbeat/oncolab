import shlex
import sys


def fake_colab(root):
    """Use a fake Python launcher so the installed CLI is exercised offline."""
    launcher = root / "python"
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(root / 'allocator.py'))} \"$@\"\n"
    )
    launcher.chmod(0o755)
    (root / "allocator.py").write_text("""
import json, os, sys, time
from pathlib import Path
assert sys.argv[1] == "-I" and sys.argv[2].endswith("/allocation_worker.py")
mode = os.environ.get("TEST_ALLOCATION", "success")
if mode == "crash":
    print("\033[31mTraceback (most recent call last):\033[0m; secret-token", file=sys.stderr, flush=True)
    sys.exit(2)
if mode == "login":
    print(json.dumps({"error": "No usable Colab credentials. Run 'colab sessions' in a terminal to sign in."}), flush=True)
    sys.exit(1)
if mode == "cancel":
    print(json.dumps({"label": "Waiting to retry…", "detail": "TEST_BACKOFF_STARTED", "deadline": time.monotonic() + 40}), flush=True)
    if ready := os.environ.get("TEST_ALLOCATION_READY"):
        Path(ready).touch()
    try:
        time.sleep(40)
    except KeyboardInterrupt:
        sys.exit(130)
store, name = sys.argv[3:5]
Path(store).write_text(json.dumps({name: {}}))
""")
    colab = root / "colab"
    colab.write_text(f"#!{launcher}\n")
    colab.chmod(0o755)
