import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios

from prompt_toolkit.utils import get_cwidth
import time
import unittest
from unittest.mock import patch

from oncolab.diagnostics import outcome
from oncolab.duration import format_duration
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
        pid, fd = pty.fork()
        if not pid:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
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
            rendered = bytes(output)
            text = rendered.decode()
            def column(label):
                match = re.search(label + r"[^\r\n]*?(?=\x1b\[0;36m\s*\d\.\ds)", text)
                self.assertIsNotNone(match, rendered)
                prefix = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m[1] or 1), match[0])
                prefix = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", prefix)
                return get_cwidth(prefix)
            expected_column = get_cwidth("Long planned step with 漢") + 2
            self.assertEqual(column("Waiting"), expected_column)
            self.assertEqual(column("Short"), expected_column)
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
            self.assertRegex(plain, r"CPU · attempt 2/4 · 3\.0d · time out[^\r\n]*[\r\n]")
            self.assertRegex(rendered, rb"\x1b\[J[^\r\n]*READY_FOR_COMMAND")
        finally:
            os.close(fd)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
