import errno
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
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
import json, os, shlex, sys, time
from pathlib import Path
argv = sys.argv[1:]
if "-O" in argv:
    raise SystemExit(0)
if "-M" in argv:
    if os.environ.get("TEST_SSH_FAILURE"):
        raise SystemExit(255)
    assert not sys.stdin.buffer.read(), "SSH bootstrap consumed payload stdin"
    if os.environ.get("TEST_SSH_AUTH_PROMPT"):
        assert "BatchMode=yes" not in argv
        with open("/dev/tty") as terminal:
            (Path(os.environ["XDG_CONFIG_HOME"]) / "waiting-for-passphrase").touch()
            assert terminal.readline() == "fixture phrase\n"
    raise SystemExit(0)
if "-T" in argv:
    if shlex.split(argv[-1])[-1] == "fail-with-traceback":
        print("Traceback (most recent call last):\nRuntimeError: user failure", file=sys.stderr)
        raise SystemExit(19)
    if shlex.split(argv[-1])[-1] == "transport-255":
        Path(argv[argv.index("-E") + 1]).write_text("mux_client_request_session: read from master failed: Broken pipe")
        raise SystemExit(255)
    if shlex.split(argv[-1])[-1] == "exit-255":
        print("user output")
        raise SystemExit(255)
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    raise SystemExit(0)
assert "-t" in argv and os.isatty(0)
root = Path(os.environ["XDG_CONFIG_HOME"])
# The delay ends only when the terminal reader observes the indicator.
while not (root / "finish-bootstrap").exists():
    time.sleep(.02)
if os.environ.get("TEST_TTY_START_FAILURE"):
    print("oops", file=sys.stderr)
    raise SystemExit(7)
if os.environ.get("TEST_TTY_RAW_OUTPUT"):
    import termios, tty
    original = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        os.write(1, b"RAW_FIRST\r\nRAW_LAST\r\n")
    finally:
        termios.tcsetattr(0, termios.TCSANOW, original)
    raise SystemExit(19)
parts = shlex.split(argv[-1])
assert parts[-1] == "arg with $ and spaces"
source, stage, color, plan, *_ = parts[5:]
config = json.loads(plan)
config["workdir"] = str(root)
namespace = {"__name__": "fixture", "__package__": None}
exec(source, namespace)
namespace["unassign"] = lambda: None
script = 'assert input("READY_FOR_TEST: ") == "passed through the tty"; print("PTY_ROUND_TRIP_OK")'
sys.argv = ["-c", stage, color, json.dumps(config), sys.executable, "-c", script]
raise SystemExit(namespace["main"]())
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

    def test_payload_exit_255_does_not_add_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            oncolab, env = self.fake_commands(Path(tmp))
            result = subprocess.run([oncolab, "--", "exit-255"], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=12)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (255, b"user output\n", b""))

    def test_ssh_exit_255_with_native_error_reports_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            oncolab, env = self.fake_commands(Path(tmp))
            result = subprocess.run([oncolab, "--", "transport-255"], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=12)
            self.assertEqual((result.returncode, result.stdout), (255, b""))
            self.assertEqual(result.stderr, b"[SSH connection failed] The SSH session could not be opened. "
                                            b"Check 'colab sessions' for the VM.\n")

    def test_delayed_ssh_handoff(self):
        for outcome in ("success", "failure", "interrupt", "raw"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                oncolab, env = self.fake_commands(root)
                if outcome == "failure":
                    env["TEST_TTY_START_FAILURE"] = "1"
                if outcome == "raw":
                    env["TEST_TTY_RAW_OUTPUT"] = "1"
                if outcome == "success":
                    env.update(TEST_SSH_AUTH_PROMPT="1", HOME=str(root))
                pid, fd = pty.fork()
                if not pid:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                    os.execve(oncolab, [oncolab, "--", "example", "arg with $ and spaces"], env)
                output = bytearray()
                deadline = time.monotonic() + 12
                answered = False
                auth_answered = False
                saw_handoff = False
                try:
                    while time.monotonic() < deadline:
                        if not auth_answered and (root / "waiting-for-passphrase").exists():
                            self.assertNotIn(b"Preparing remote command", output)
                            os.write(fd, b"fixture phrase\n")
                            auth_answered = True
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
                        if not saw_handoff and b"Preparing remote command" in output:
                            saw_handoff = True
                            if outcome == "interrupt":
                                # Send SIGINT only to oncolab, not SSH, to
                                # detect a blocked reader during cancellation.
                                os.kill(pid, signal.SIGINT)
                            else:
                                (root / "finish-bootstrap").touch()
                        if not answered and b"READY_FOR_TEST" in output:
                            os.write(fd, b"passed through the tty\n")
                            answered = True
                    else:
                        self.fail(f"PTY timed out: {output!r}")
                    _, status = os.waitpid(pid, 0)
                    pid = None
                    self.assertTrue(saw_handoff, output)
                    expected = {"success": 0, "failure": 7, "interrupt": 130, "raw": 19}[outcome]
                    self.assertEqual(os.waitstatus_to_exitcode(status), expected, output)
                    if outcome == "success":
                        self.assertTrue(auth_answered, output)
                        self.assertIn(b"PTY_ROUND_TRIP_OK", output)
                        self.assertRegex(output, rb"\x1b\[A\r.*?\x1b\[J[^\r\n]*READY_FOR_TEST")
                    elif outcome == "failure":
                        self.assertRegex(output, rb"Preparing remote command[^\r\n]*\r\noops\r\n")
                    elif outcome == "raw":
                        self.assertTrue(output.endswith(b"RAW_FIRST\r\nRAW_LAST\r\n"), output)
                    else:
                        self.assertIn(b"[cancelled]", output)
                finally:
                    os.close(fd)
                    if pid:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
