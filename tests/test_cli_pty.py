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
import json, os, shlex, signal, subprocess, sys, time
from pathlib import Path
argv = sys.argv[1:]
if "-O" in argv:
    raise SystemExit(0)
if "-M" in argv:
    root = Path(os.environ["XDG_CONFIG_HOME"])
    for i, arg in enumerate(argv[:-1]):
        if arg == "-L":
            local, remote = argv[i + 1].split(":", 1)
            if local.endswith("/summary.sock"):
                Path(local).symlink_to(root / "remote-summary")
    if os.environ.get("TEST_SSH_HANG"):
        # Model a wedged client plus a proxy that inherited SSH's stderr.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        proxy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (root / "hung-ssh").write_text(f"{os.getpid()} {proxy.pid}")
        while True:
            time.sleep(1)
    if mode := os.environ.get("TEST_SSH_RETRY"):
        log = root / "bootstrap-stores"
        count = len(log.read_text().splitlines()) if log.exists() else 0
        proxy = shlex.split(next(arg for arg in argv if arg.startswith("ProxyCommand=")))
        store = proxy[2]
        with log.open("a") as stream:
            print(store, file=stream)
        if count < 2 and mode in {"transient", "unknown"}:
            diagnosis = ("kex_exchange_identification: Connection closed by remote host"
                         if mode == "transient" else "redact me: token=secret-fixture")
            print(diagnosis, file=sys.stderr)
            raise SystemExit(255)
        if mode == "auth":
            print("root@colab-runtime: Permission denied (publickey).", file=sys.stderr)
            raise SystemExit(255)
        if mode == "gone":
            print("[colab] Error: No active sessions found. Create one with 'colab new'.", file=sys.stderr)
            raise SystemExit(255)
        if mode == "proxy-auth":
            assert proxy[-2] == "2>"
            log_path = Path(proxy[-1])
            assert log_path.stat().st_mode & 0o777 == 0o600
            log_path.write_text("x" * 50000 + "\n[colab] Server rejected pubkey: fixture-secret\n")
            print("kex_exchange_identification: Connection closed by remote host", file=sys.stderr)
            raise SystemExit(255)
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
        Path(argv[argv.index("-E") + 1]).write_text("mux.c:mux_client_request_session():1982 (pid=8): "
                                                     "read from master failed: Broken pipe")
        raise SystemExit(255)
    if shlex.split(argv[-1])[-1] == "transport-lost":
        assert "LogLevel=ERROR" in argv and "LogVerbose=*:mux_client_request_session():*" in argv
        Path(argv[argv.index("-E") + 1]).write_text("debug2: mux_client_request_session: master session id: 2\n"
                                                     "debug2: Control master terminated unexpectedly")
        raise SystemExit(255)
    if shlex.split(argv[-1])[-1] == "exit-255":
        Path(argv[argv.index("-E") + 1]).write_text("debug2: Received exit status from master 255")
        print("user output")
        print("mux_client_request_session: this is payload stderr", file=sys.stderr)
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
if config.get("summary_path"):
    config["summary_path"] = str(root / "remote-summary")
if os.environ.get("TEST_TTY_SETUP_FAILURE"):
    config["setup"] = {
        "fail": {"description": "Failing test hook", "needs": [],
                 "argv": [sys.executable, "-c", "import sys,time; time.sleep(.35); "
                          "print('HOOK_FIRST', file=sys.stderr); print('HOOK_LAST', file=sys.stderr); "
                          "raise SystemExit(7)"]},
    }
namespace = {"__name__": "fixture", "__package__": None}
exec(source, namespace)
namespace["unassign"] = lambda: None
# Keep the fixture prompt on stdout. With fd 2 redirected, Python
# can select a different input() path than it would on a real SSH PTY.
script = ('print("READY_FOR_TEST: ", end="", flush=True); '
          'assert input() == "passed through the tty"; print("PTY_ROUND_TRIP_OK")')
sys.argv = ["-c", stage, color, json.dumps(config), sys.executable, "-c", script]
raise SystemExit(namespace["main"]())
''')
        ssh.chmod(0o755)
        env = dict(os.environ, ONCOLAB_PARALLEL_SETUP="0", PATH=f"{root}:{os.environ['PATH']}", NO_COLOR="1",
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
                result = subprocess.run([oncolab, "--attempts", "1", "--", "true"], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=writer, timeout=8)
            finally:
                os.close(writer)
            self.assertEqual(result.returncode, 255)
            self.assertTrue(released.is_file())

    def test_ssh_retry_uses_same_vm_and_auth_errors_stop(self):
        for mode, attempts, expected_calls, succeeded, message in (
            ("transient", "0", 3, True, b""),
            ("unknown", "0", 3, True, b""),
            ("transient", "2", 2, False, b"The SSH connection closed."),
            ("auth", "0", 1, False, b"check your SSH key"),
            ("gone", "0", 1, False, b"Colab no longer has an active session"),
            ("proxy-auth", "0", 1, False, b"Colab rejected your public key"),
        ):
            with self.subTest(mode=mode, attempts=attempts), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                oncolab, env = self.fake_commands(root)
                released = root / "released"
                env.update(TEST_SSH_RETRY=mode, COLAB_CLI_RETRY_BACKOFF="0", TEST_RELEASE_MARKER=str(released))
                result = subprocess.run([oncolab, "--attempts", attempts, "--", "cat"], env=env, input=b"hello\n",
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0 if succeeded else 255, result.stderr)
                self.assertEqual(result.stdout, b"hello\n" if succeeded else b"")
                self.assertIs(released.is_file(), not succeeded)
                stores = (root / "bootstrap-stores").read_text().splitlines()
                self.assertEqual(len(stores), expected_calls)
                self.assertEqual(len(set(stores)), 1)
                if succeeded:
                    self.assertEqual(result.stderr, b"")
                else:
                    self.assertIn(message, result.stderr)
                self.assertNotIn(b"fixture-secret", result.stderr)

    def test_ctrl_c_during_unlimited_ssh_backoff_releases_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oncolab, env = self.fake_commands(root)
            released = root / "released"
            env.update(TEST_SSH_RETRY="unknown", TEST_SSH_FAILURE="1", COLAB_CLI_RETRY_BACKOFF="120",
                       TEST_RELEASE_MARKER=str(released))
            with subprocess.Popen([oncolab, "--attempts", "0", "--", "true"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                try:
                    log = root / "bootstrap-stores"
                    stop = time.monotonic() + 4
                    while not log.exists() and proc.poll() is None and time.monotonic() < stop:
                        time.sleep(.02)
                    self.assertTrue(log.exists())
                    time.sleep(.3)  # Let the fake SSH exit and the retry backoff begin.
                    proc.send_signal(signal.SIGINT)
                    output, errors = proc.communicate(timeout=5)
                    self.assertEqual((proc.returncode, output, errors), (130, b"", b"[cancelled]\n"))
                    self.assertTrue(released.is_file())
                    self.assertEqual(len(log.read_text().splitlines()), 1)
                finally:
                    if proc.poll() is None:
                        proc.kill()

    def test_targeted_sigint_during_inflight_ssh_reaps_client_and_releases_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oncolab, env = self.fake_commands(root)
            released = root / "released"
            env.update(TEST_SSH_HANG="1", TEST_RELEASE_MARKER=str(released))
            with subprocess.Popen([oncolab, "--attempts", "0", "--", "true"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                ssh_pid = proxy_pid = None
                try:
                    marker = root / "hung-ssh"
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and proc.poll() is None:
                        if marker.exists() and (pids := marker.read_text().split()):
                            ssh_pid, proxy_pid = map(int, pids)
                            break
                        time.sleep(.02)
                    self.assertIsNotNone(ssh_pid)
                    # Signal oncolab only, not the terminal's process group.
                    proc.send_signal(signal.SIGINT)
                    output, errors = proc.communicate(timeout=8)
                    self.assertEqual((proc.returncode, output, errors), (130, b"", b"[cancelled]\n"))
                    self.assertTrue(released.is_file())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(ssh_pid, 0)
                finally:
                    for pid in (ssh_pid, proxy_pid):
                        if pid is not None:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    if proc.poll() is None:
                        proc.kill()

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
            self.assertEqual((result.returncode, result.stdout, result.stderr), (255, b"user output\n", b"mux_client_request_session: this is payload stderr\n"))

    def test_ssh_exit_255_with_native_error_reports_recovery(self):
        for name, explanation in (
            ("transport-255", "The SSH session could not be opened."),
            ("transport-lost", "The SSH connection closed before reporting an exit status."),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                oncolab, env = self.fake_commands(Path(tmp))
                result = subprocess.run([oncolab, "--attempts", "0", "--", name], env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=12)
                self.assertEqual((result.returncode, result.stdout), (255, b""))
                self.assertEqual(result.stderr, (f"[SSH connection failed] {explanation} "
                                                 "Check 'colab sessions' for the VM.\n").encode())

    def test_delayed_ssh_handoff(self):
        for outcome in ("success", "failure", "interrupt", "raw", "setup-failure", "verbose"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                oncolab, env = self.fake_commands(root)
                if outcome == "failure":
                    env["TEST_TTY_START_FAILURE"] = "1"
                    env.pop("NO_COLOR", None)
                if outcome == "raw":
                    env["TEST_TTY_RAW_OUTPUT"] = "1"
                if outcome == "setup-failure":
                    env["TEST_TTY_SETUP_FAILURE"] = "1"
                if outcome == "success":
                    env.update(TEST_SSH_AUTH_PROMPT="1", HOME=str(root), PYTHONIOENCODING="ascii")
                if outcome == "verbose":
                    import socket

                    with socket.socket(socket.AF_UNIX) as probe:
                        try:
                            probe.bind(str(root / "socket-test"))
                        except OSError as exc:
                            if exc.errno in (errno.EPERM, errno.EACCES):
                                self.skipTest("sandbox does not allow Unix sockets")
                            raise
                pid, fd = pty.fork()
                if not pid:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                    extra = []
                    if outcome == "verbose":
                        stderr = os.open(root / "diagnostics", os.O_WRONLY | os.O_CREAT, 0o600)
                        os.dup2(stderr, 2)
                        os.close(stderr)
                        extra = ["--verbose"]
                    os.execve(oncolab, [oncolab, *extra, "--", "example", "arg with $ and spaces"], env)
                output = bytearray()
                deadline = time.monotonic() + 12
                answered = False
                auth_answered = False
                saw_handoff = False
                try:
                    while time.monotonic() < deadline:
                        if not auth_answered and (root / "waiting-for-passphrase").exists():
                            self.assertNotIn(b"Waiting for remote setup to start", output)
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
                        if not saw_handoff and b"Waiting for remote setup to start" in output:
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
                    expected = {"success": 0, "failure": 7, "interrupt": 130, "raw": 19,
                                "setup-failure": 7, "verbose": 0}[outcome]
                    self.assertEqual(os.waitstatus_to_exitcode(status), expected, output)
                    if outcome == "success":
                        self.assertTrue(auth_answered, output)
                        self.assertIn(b"o Waiting for remote setup to start...\r\n", output)
                        self.assertIn(b"PTY_ROUND_TRIP_OK", output)
                        self.assertRegex(output, rb"\x1b\[A\r.*?\x1b\[J[^\r\n]*READY_FOR_TEST")
                    elif outcome == "verbose":
                        self.assertIn(b"PTY_ROUND_TRIP_OK", output)
                        self.assertNotIn("✓".encode(), output)
                        diagnostics = (root / "diagnostics").read_text()
                        for phase in ("Creating Colab VM…", "Connecting via SSH…", "Setting up…"):
                            self.assertIn("✓ " + phase, diagnostics)
                    elif outcome == "failure":
                        self.assertIn("\x1b[37m⠛\x1b[0;2m Waiting for remote setup to start…\x1b[0m\r\noops\r\n".encode(), output)
                    elif outcome == "setup-failure":
                        self.assertRegex(output, rb"(?s)Failing test hook.*?\x1b\[J[^\r\n]*HOOK_FIRST\r\n"
                                                 rb"HOOK_LAST\r\n\[Setup failed\] Failing test hook \(exit 7\).")
                        self.assertNotIn(b"READY_FOR_TEST", output)
                    elif outcome == "raw":
                        self.assertIn("⠛ Waiting for remote setup to start…\r\n".encode(), output)
                        self.assertTrue(output.endswith(b"RAW_FIRST\r\nRAW_LAST\r\n"), output)
                    else:
                        self.assertIn(b"[cancelled]", output)
                finally:
                    os.close(fd)
                    if pid:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
