import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import socket
import tempfile
import threading
import subprocess
import sys
import termios

from prompt_toolkit.utils import get_cwidth
import time
import unittest
from unittest.mock import patch

from oncolab.diagnostics import outcome
from oncolab.duration import format_duration
from oncolab.cli import read_setup_summary
from oncolab.progress import SetupTimeline, display_label, setup_summary_socket
from oncolab.remote import Lifecycle


class Progress(unittest.TestCase):
    def test_durations_use_one_decimal_and_switch_units(self):
        for seconds, expected in (
            (-1, "0.0s"), (0, "0.0s"), (59.9, "59.9s"), (60, "1.0m"),
            (90, "1.5m"), (3594, "59.9m"), (3600, "1.0h"), (5400, "1.5h"),
            (86040, "23.9h"), (86400, "1.0d"), (129600, "1.5d"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_rounded_elapsed_time_carries_to_the_next_unit(self):
        for seconds, expected in (
            (59.94, "59.9s"), (59.95, "1.0m"),
            (3596.9, "59.9m"), (3597, "1.0h"),
            (86219, "23.9h"), (86220, "1.0d"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_rounded_remaining_time_never_understates_the_wait(self):
        for seconds, expected in (
            (0, "0.0s"), (0.001, "0.1s"),
            (59.9, "59.9s"), (59.91, "1.0m"), (60.01, "1.1m"),
            (3594, "59.9m"), (3594.1, "1.0h"), (3601, "1.1h"),
            (86040, "23.9h"), (86041, "1.0d"), (86401, "1.1d"),
            (90000, "1.1d"), (95040, "1.1d"), (95040.01, "1.2d"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds, round_up=True), expected)

    def test_outcome_only_colors_the_heading_on_a_terminal(self):
        with patch("sys.stderr.isatty", return_value=True), patch.dict(os.environ, TERM="xterm"):
            self.assertEqual(outcome("VM creation failed", "Unavailable.", color=True),
                             "\x1b[31m[VM creation failed]\x1b[0m Unavailable.")
            self.assertEqual(outcome("VM creation failed", "Unavailable.", color=False),
                             "[VM creation failed] Unavailable.")

    def test_deferred_setup_output_tolerates_a_closed_terminal(self):
        lifecycle = Lifecycle()
        lifecycle.setup_output = [b"fixture\n"]
        with patch("sys.stderr.fileno", return_value=2), patch("oncolab.remote.os.write", side_effect=BrokenPipeError) as write:
            lifecycle.flush_setup_output()
        write.assert_called_once_with(2, b"fixture\n")
        self.assertIsNone(lifecycle.setup_output)

    def test_closed_summary_destination_does_not_replace_command_exit(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            result = subprocess.run(
                [sys.executable, "-c", "from oncolab.progress import completed_steps; "
                 "completed_steps([(0, 'Setting up…', 0, 1)], ['Setting up…'], False); "
                 "print('command output'); raise SystemExit(37)"],
                stdout=subprocess.PIPE, stderr=writer, timeout=5,
            )
        finally:
            os.close(writer)
        self.assertEqual((result.returncode, result.stdout), (37, b"command output\n"))

    def test_missing_standard_error_is_ignored(self):
        from oncolab.progress import completed_steps

        with patch("sys.stderr", None):
            completed_steps([(0, "Setting up…", 0, 1)], ["Setting up…"], False)

    def test_full_summary_pipe_does_not_block_command(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(writer, b"x" * 512)
            os.set_blocking(writer, True)
            result = subprocess.run(
                [sys.executable, "-c", "from oncolab.progress import completed_steps; "
                 "completed_steps([(0, 'Setting up…', 0, 1)], ['Setting up…'], False); "
                 "print('command output'); raise SystemExit(37)"],
                stdout=subprocess.PIPE, stderr=writer, timeout=5,
            )
            self.assertEqual((result.returncode, result.stdout), (37, b"command output\n"))
        finally:
            os.close(reader)
            os.close(writer)

    def test_full_stderr_socket_does_not_block_command(self):
        connector, sink = socket.socketpair()
        with connector, sink:
            sink.setblocking(False)
            with self.assertRaises(BlockingIOError):
                while True:
                    sink.send(b"x" * 512)
            sink.setblocking(True)
            result = subprocess.run(
                [sys.executable, "-c", "from oncolab.progress import completed_steps; "
                 "completed_steps([(0, 'Setting up…', 0, 1)], ['Setting up…'], False); "
                 "print('command output'); raise SystemExit(37)"],
                stdout=subprocess.PIPE, stderr=sink, timeout=5,
            )
        self.assertEqual((result.returncode, result.stdout), (37, b"command output\n"))

    def test_stopped_stderr_terminal_does_not_block_command(self):
        reader, writer = pty.openpty()
        try:
            os.set_blocking(writer, False)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(writer, b"x" * 512)
            os.set_blocking(writer, True)
            result = subprocess.run(
                [sys.executable, "-c", "from oncolab.progress import completed_steps; "
                 "completed_steps([(0, 'Setting up…', 0, 1)], ['Setting up…'], False); "
                 "print('command output'); raise SystemExit(37)"],
                stdout=subprocess.PIPE, stderr=writer, timeout=5, env=dict(os.environ, TERM="xterm"),
            )
            self.assertEqual((result.returncode, result.stdout), (37, b"command output\n"))
        finally:
            os.close(reader)
            os.close(writer)

    def test_control_characters_in_job_labels_stay_on_one_line(self):
        self.assertEqual(display_label("line 1\nline 2\tfoo\x1b[31m"), "line 1 line 2 foo\\x1b[31m")

    def test_forwarded_setup_summary_acknowledges_after_the_local_write(self):
        from contextlib import contextmanager
        from io import StringIO

        with tempfile.TemporaryDirectory(prefix="oc-summary-", dir="/tmp") as directory:
            path = directory + "/summary.sock"
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.bind(directory + "/probe")
                except OSError as exc:
                    if exc.errno in (errno.EPERM, errno.EACCES):
                        self.skipTest("sandbox does not allow Unix sockets")
                    raise
            stop = threading.Event()
            stderr = StringIO()
            with patch("sys.stderr", stderr), contextmanager(setup_summary_socket)(path) as send:
                reader = threading.Thread(target=read_setup_summary, args=(path, stop, ["Setting up…"], False))
                reader.start()
                try:
                    send([(0, "Setting up…", 0, 2)])
                    self.assertRegex(stderr.getvalue(), r"^✓ Setting up… +2\.0s\n$")
                finally:
                    stop.set()
                    reader.join(3)
                self.assertFalse(reader.is_alive())
            self.assertFalse(os.path.exists(path))

    def test_setup_checks_follow_readiness_and_drop_eliminated_branches(self):
        timeline = SetupTimeline()
        event = timeline.event
        event("setup-start", 0)
        for name, dependencies in (("rclone", ()), ("config", ()), ("git", ()),
                                   ("mount", ("rclone", "config")), ("seal", ("mount",))):
            event("setup-job", name, name if name != "seal" else None, dependencies)
        for name in ("rclone", "config", "git"):
            event("setup-active", name, 0)
        event("setup-done", "rclone", 4, 0)
        self.assertEqual(timeline.candidates(), set())
        event("setup-done", "config", 6, 0)
        event("setup-active", "mount", 6)
        self.assertEqual(timeline.candidates(), {"config"})
        self.assertEqual([name for _, name, *_ in timeline.rows(False)], ["Setting up…", "git", "mount"])
        event("setup-done", "mount", 7, 0)
        event("setup-active", "seal", 7)
        event("setup-done", "git", 8, 0)
        event("setup-done", "seal", 9, 0)
        event("setup-finish", 9, 0)
        self.assertEqual(timeline.rows(True), [(0, "Setting up…", 0, 9), (2, "config", 0, 6), (2, "mount", 6, 7)])
        self.assertEqual(timeline.rows(False), [])
        event("setup-finish", 9, 130)
        self.assertEqual(timeline.rows(True), [])
        event("setup-done", "git", 8, 7)
        self.assertEqual(timeline.candidates(), set())

    def test_tty_setup_has_a_parent_in_both_modes_and_retains_only_verbose_checks(self):
        script = """
import asyncio
from time import monotonic
from oncolab.progress import progress

async def exercise(run):
    event = run.setup_event
    started = monotonic()
    event("setup-start", started)
    event("setup-job", "first", "First job…", ())
    event("setup-job", "next", "Next job…", ("first",))
    event("setup-active", "first", started)
    await asyncio.sleep(.25)
    event("setup-done", "first", monotonic(), 0)
    event("setup-active", "next", monotonic())
    await asyncio.sleep(.25)
    event("setup-done", "next", monotonic(), 0)
    event("setup-finish", monotonic(), 0)

progress(exercise, True, False, setup=True, verbose=VERBOSE,
         labels=["Setting up…", "  First job…", "  Next job…"])
print("COMMAND_OUTPUT")
"""
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                raw = self.render_progress(script.replace("VERBOSE", repr(verbose)), 60).decode()
                plain = self.without_ansi(raw)
                self.assertIn("Setting up…", plain)
                self.assertRegex(plain, r"  [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] First job…")
                self.assertIn("Next job…", plain)
                if verbose:
                    self.assertRegex(plain, r"✓ Setting up… +0\.\ds\r?\n  ✓ First job… +0\.\ds\r?\n"
                                            r"  ✓ Next job… +0\.\ds\r?\nCOMMAND_OUTPUT")
                else:
                    self.assertNotIn("✓", plain)
                    self.assertRegex(raw, r"\x1b\[J[^\r\n]*COMMAND_OUTPUT")

    def test_tty_setup_steps_share_elapsed_column_and_leave_no_final_row(self):
        script = """
import asyncio
import inspect
import subprocess as sp
import sys
from time import monotonic
from oncolab.allocation import AllocationLabel
from oncolab.duration import format_duration as installed_format_duration
from oncolab.progress import progress as installed_progress
# The VM gets these functions' source, not the installed modules.
exec(inspect.getsource(installed_format_duration))
exec(inspect.getsource(installed_progress))
allocation = AllocationLabel()
allocation.state = ("Waiting…", "time out", monotonic() + 3 * 86400, "CPU · attempt 2/4")
labels = ["Short…", "Long planned step with 漢", "Waiting…"]
async def wait():
    await asyncio.sleep(.55)
progress(lambda run: run(allocation, wait), True, True, labels=labels)
progress(lambda run: run("Short…", wait), True, True, labels=labels)
print("READY_FOR_COMMAND")
"""
        rendered = self.render_progress(script, 40)
        text = rendered.decode()

        def column(label):
            match = re.search(label + r"[^\r\n]*?(?=\x1b\[0;36m\s*\d\.\ds)", text)
            self.assertIsNotNone(match, rendered)
            prefix = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m[1] or 1), match[0])
            return get_cwidth(self.without_ansi(prefix))

        expected_column = get_cwidth("Long planned step with 漢") + 2
        self.assertEqual(column("Waiting"), expected_column)
        self.assertEqual(column("Short"), expected_column)
        self.assertRegex(self.without_ansi(text), r"CPU · attempt 2/4 · 3\.0d · time out[^\r\n]*[\r\n]")
        self.assertRegex(rendered, rb"\x1b\[J[^\r\n]*READY_FOR_COMMAND")

    def test_tty_retry_wait_wraps_target_attempt_countdown_and_reason(self):
        script = """
import asyncio
from time import monotonic
from oncolab.allocation import AllocationLabel
from oncolab.progress import WAITING, preparation_labels, progress

async def wait():
    await asyncio.sleep(1.35)
async def exercise(run):
    allocation = AllocationLabel()
    allocation.state = (WAITING, "Colab request timed out while checking the requested accelerator",
                        monotonic() + 10.8, "CPU High-RAM · attempt 2/4")
    await run(allocation, wait)
progress(exercise, True, True, labels=preparation_labels({}))
print("READY_FOR_COMMAND")
"""
        for width in (32, 40):
            with self.subTest(width=width):
                rendered = self.render_progress(script, width)
                plain = self.without_ansi(rendered.decode())
                lines = [line.strip() for line in plain.splitlines() if line.strip()]
                start = next(i for i, line in enumerate(lines) if line.startswith("CPU High-RAM"))
                end = next(i for i in range(start, len(lines)) if "accelerator" in lines[i])
                self.assertGreaterEqual(end - start, 2, rendered)
                self.assertRegex(" ".join(lines[start:end + 1]),
                                 r"^CPU High-RAM · attempt 2/4 · 10\.\ds · Colab request timed out "
                                 r"while checking the requested accelerator$")
                self.assertRegex(rendered, rb"\x1b\[(?:0;)?33mColab")
                self.assertNotRegex(rendered, rb"\x1b\[(?:0;)?33mCPU")
                # The terminal gets only changed cells in later frames.
                self.assertRegex(plain, r"9\.\ds\b")
                self.assertRegex(plain, r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Waiting to retry…[^\r\n]*\d\.\ds")
                self.assertRegex(rendered, rb"\x1b\[J[^\r\n]*READY_FOR_COMMAND")

    @staticmethod
    def without_ansi(value):
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)

    def render_progress(self, script, width):
        pid, fd = pty.fork()
        if not pid:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
            os.execve(sys.executable, [sys.executable, "-c", script],
                      dict(os.environ, TERM="xterm-256color"))
        output = bytearray()
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not select.select([fd], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(fd, 8192)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                output.extend(data)
            else:
                self.fail("timed progress did not exit")
            _, status = os.waitpid(pid, 0)
            pid = None
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, bytes(output))
            self.assertNotIn(b"Traceback", output)
            return bytes(output)
        finally:
            os.close(fd)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
