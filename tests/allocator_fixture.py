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
if Path(sys.argv[1]).name == "colab":
    del sys.argv[1]
if sys.argv[1:2] == ["--config"] and sys.argv[3:4] == ["stop"]:
    Path(os.environ["TEST_RELEASE_MARKER"]).touch()
    print("[colab] Session terminated.")
    sys.exit(0)
assert sys.argv[1] == "-I" and sys.argv[2].endswith("/allocation_worker.py")
mode = os.environ.get("TEST_ALLOCATION", "success")
if mode == "crash":
    print("Traceback (most recent call last):", file=sys.stderr)
    print("│ /fake/auth.py:12 in open_session │", file=sys.stderr)
    print("RuntimeError: secret-token", file=sys.stderr, flush=True)
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
    colab.write_text("#!/bin/sh\n" + "'''exec' " + shlex.quote(str(launcher)) + ' "$0" "$@"\n' + "' '''\n")
    colab.chmod(0o755)
