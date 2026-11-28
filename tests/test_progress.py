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
import time
import unittest


class Progress(unittest.TestCase):
    def test_tty_setup_steps_share_elapsed_column_and_leave_no_final_row(self):
        script = """
import asyncio
from oncolab.progress import progress
async def work(run):
    run.reserve("Long planned step with 漢")
    async def wait():
        await asyncio.sleep(.8)
    await run("Short…", wait)
progress(work, True, True)
print("READY_FOR_COMMAND")
"""
        pid, fd = pty.fork()
        if not pid:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
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
            # Prompt Toolkit moves the cursor across the reserved gap on its first paint.
            self.assertRegex(rendered, rb"Short[^\r\n]*?(?:\x1b\[\d+C| {8,})[^\r\n]*?\x1b\[0;36m\s*\d\.\ds")
            self.assertRegex(rendered, rb"\x1b\[J[^\r\n]*READY_FOR_COMMAND")
        finally:
            os.close(fd)
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
