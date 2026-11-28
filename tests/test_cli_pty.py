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

from allocator_fixture import fake_colab


class Terminal(unittest.TestCase):
    def fake_commands(self, root):
        oncolab = shutil.which("oncolab")
        self.assertIsNotNone(oncolab)
        fake_colab(root)
        ssh = root / "ssh"
        ssh.write_text(f"#!{sys.executable}\n" + r'''
import os, shlex, sys
argv = sys.argv[1:]
if "-O" in argv:
    raise SystemExit(0)
if "-M" in argv:
    if os.environ.get("TEST_SSH_FAILURE"):
        raise SystemExit(255)
    assert not sys.stdin.buffer.read(), "SSH bootstrap consumed payload stdin"
    raise SystemExit(0)
if "-T" in argv:
    if shlex.split(argv[-1])[-1] == "fail-with-traceback":
        print("Traceback (most recent call last):\nRuntimeError: user failure", file=sys.stderr)
        raise SystemExit(19)
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    raise SystemExit(0)
assert "-t" in argv and os.isatty(0) and os.isatty(1)
parts = shlex.split(argv[-1])
assert parts[-1] == "arg with $ and spaces"
assert input("READY_FOR_TEST: ") == "passed through the tty"
print("PTY_ROUND_TRIP_OK")
''')
        ssh.chmod(0o755)
        env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", NO_COLOR="1",
                   TERM="xterm-256color", XDG_CONFIG_HOME=str(root))
        return oncolab, env

    def test_installed_cli_preserves_piped_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            oncolab, env = self.fake_commands(Path(tmp))
            data = b"payload stdin\n"
            result = subprocess.run([oncolab, "--", "cat"], env=env, input=data,
                                    capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, data)

    def test_failed_ssh_releases_vm_even_when_stderr_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oncolab, env = self.fake_commands(root)
            released = root / "released"
            env.update(TEST_SSH_FAILURE="1", TEST_RELEASE_MARKER=str(released))
            reader, writer = os.pipe()
            os.close(reader)
            try:
                result = subprocess.run([oncolab, "--", "true"], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=writer, timeout=8)
            finally:
                os.close(writer)
            self.assertEqual(result.returncode, 255)
            self.assertTrue(released.is_file())

    def test_command_traceback_passes_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            oncolab, env = self.fake_commands(Path(tmp))
            result = subprocess.run([oncolab, "--", "fail-with-traceback"], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 19)
            self.assertEqual(result.stderr, b"Traceback (most recent call last):\nRuntimeError: user failure\n")

    def test_installed_cli_preserves_pty_and_argument_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            oncolab, env = self.fake_commands(Path(tmp))
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
