import errno
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class Terminal(unittest.TestCase):
    def test_installed_cli_preserves_pty_and_argument_boundaries(self):
        oncolab = shutil.which("oncolab")
        self.assertIsNotNone(oncolab)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            colab = root / "colab"
            colab.write_text("#!/bin/sh\nprintf \"[colab] Creating session 'test1'...\\n\"\n")
            colab.chmod(0o755)
            ssh = root / "ssh"
            ssh.write_text(f"#!{sys.executable}\n" + r'''
import os, shlex, sys
argv = sys.argv[1:]
if "-O" in argv or "-M" in argv:
    raise SystemExit(0)
assert "-t" in argv and os.isatty(0) and os.isatty(1)
parts = shlex.split(argv[-1])
assert parts[-1] == "arg with $ and spaces"
assert input("READY_FOR_TEST: ") == "passed through the tty"
print("PTY_ROUND_TRIP_OK")
''')
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", NO_COLOR="1",
                       TERM="xterm-256color", XDG_CONFIG_HOME=tmp)
            pid, fd = pty.fork()
            if not pid:
                os.execve(oncolab, [oncolab, "--", "example", "arg with $ and spaces"], env)
            output = bytearray()
            deadline = time.monotonic() + 12
            answered = False
            try:
                while time.monotonic() < deadline:
                    if not select.select([fd], [], [], .25)[0]:
                        continue
                    try:
                        fragment = os.read(fd, 8192)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not fragment:
                        break
                    output.extend(fragment)
                    if not answered and b"READY_FOR_TEST" in output:
                        os.write(fd, b"passed through the tty\n")
                        answered = True
                else:
                    self.fail(f"PTY timed out: {output!r}")
                _, status = os.waitpid(pid, 0)
                pid = None
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
                self.assertIn(b"PTY_ROUND_TRIP_OK", output)
            finally:
                os.close(fd)
                if pid:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
