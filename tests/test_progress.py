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


class Progress(unittest.TestCase):
    def test_tty_setup_steps_share_elapsed_column_and_leave_no_final_row(self):
        script = """
import asyncio
from oncolab.progress import progress
class Allocation:
    def __call__(self):
        return "Waiting…"
    def detail(self):
        return "4s · 503 unavailable"
labels = ["Short…", "Long planned step with 漢", "Waiting…"]
async def wait():
    await asyncio.sleep(.55)
progress(lambda run: run(Allocation(), wait), True, True, labels=labels)
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
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
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
            self.assertIn(b"503", rendered)
            self.assertRegex(rendered, rb"\x1b\[J[^\r\n]*READY_FOR_COMMAND")
        finally:
            os.close(fd)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
