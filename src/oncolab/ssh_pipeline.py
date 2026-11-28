"""Private control socket for a setup owner and a native SSH command channel."""

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess as sp
import sys
import time


SETUP_LISTENING = b"oncolab-setup-listening\n"
CONTROL_LIMIT = 16 * 1024 * 1024


def packet(*fields):
    return (json.dumps(fields, separators=(",", ":")) + "\n").encode()


async def stdin_packets():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=CONTROL_LIMIT)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    return reader, transport


class SetupGate:
    def __init__(self, path):
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.setblocking(False)
        try:
            self.listener.bind(str(path))
            self.listener.listen(1)
        except BaseException:
            self.listener.close()
            raise
        self.stream = None
        self.peer = None
        self.pending = []

    async def accept(self):
        loop = asyncio.get_running_loop()
        self.peer, _ = await asyncio.wait_for(loop.sock_accept(self.listener), 20)
        self.peer.setblocking(True)
        self.stream = self.peer.makefile("rb")
        for item in self.pending:
            self.peer.sendall(item)
        self.pending.clear()

    def send(self, *fields):
        item = packet(*fields)
        if self.peer is not None:
            self.peer.sendall(item)
        else:
            self.pending.append(item)

    def close(self):
        self.listener.close()
        if self.stream is not None:
            self.stream.close()
        if self.peer is not None:
            self.peer.close()

    def receive_status(self):
        line = self.stream.readline(128)
        if not line:
            raise ConnectionError("SSH command channel closed")
        kind, status = json.loads(line)
        if kind != "exit" or type(status) is not int or not 0 <= status <= 255:
            raise ValueError("Invalid command status")
        return status


async def observe_setup(run, peer, buffer):
    loop = asyncio.get_running_loop()
    tasks = {}
    output = []

    async def read():
        while b"\n" not in buffer:
            block = await loop.sock_recv(peer, 65536)
            if not block:
                raise ConnectionError("SSH setup channel closed")
            buffer.extend(block)
            if len(buffer) > CONTROL_LIMIT:
                raise ValueError("SSH setup message too large")
        i = buffer.index(b"\n")
        line = bytes(buffer[:i])
        del buffer[:i + 1]
        return json.loads(line)

    try:
        while True:
            kind, *fields = await read()
            if kind == "start":
                key, label, started = fields
                done = asyncio.Event()
                tasks[key] = (done, asyncio.create_task(run(label, done.wait, started_at=started)))
            elif kind == "done":
                done, task = tasks.pop(fields[0])
                done.set()
                await task
            elif kind == "diag":
                output.append(fields[0])
            elif kind == "end-output":
                return output, await read()
            else:
                raise ValueError("Invalid setup event")
    finally:
        for done, _ in tasks.values():
            done.set()
        await asyncio.gather(*(task for _, task in tasks.values()))


def pipeline_stderr(data):
    try:
        pending = memoryview(data)
        fd = sys.stderr.fileno()
        while pending:
            written = os.write(fd, pending)
            if not written:
                break
            pending = pending[written:]
    except (OSError, ValueError):
        pass


def command_gate(path, plan, color, command, render):
    with socket.socket(socket.AF_UNIX) as peer:
        deadline = time.monotonic() + 20
        while True:
            try:
                peer.connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
        peer.setblocking(False)
        buffer = bytearray()
        live = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"

        def take_terminal():
            if plan.get("terminal_handoff"):
                print("\x1b[A\r", end="", flush=True)

        if not live and plan.get("terminal_handoff"):
            print("\x1b[A\r\x1b[K", end="", flush=True)
        output, result = render(lambda run: observe_setup(run, peer, buffer), live, color,
                                labels=plan.get("progress_labels", ()),
                                idle=plan.get("terminal_handoff"), takeover=take_terminal)
        peer.setblocking(True)
        for data in output:
            pipeline_stderr(base64.b64decode(data))
        kind, *args = result
        if kind == "run":
            workdir, env = args
            status = 1
            try:
                os.chdir(workdir)
                environment = {k: v for k, v in (os.environ | env).items() if v is not None}
                with sp.Popen(command, env=environment) as child:
                    try:
                        status = child.wait()
                    except KeyboardInterrupt:
                        # The native SSH PTY sends Ctrl+C to the command too.
                        status = child.wait()
                if status < 0:
                    status = 128 - status
            except OSError as exc:
                pipeline_stderr(f"[Command could not start] {command[0]}: {exc.strerror}.\n".encode())
                status = 127 if isinstance(exc, FileNotFoundError) else 126
            peer.sendall(packet("exit", status))
        elif kind != "failed":
            raise ValueError("Invalid setup result")
        # Keep the channel alive until the setup owner has completed cleanup.
        with peer.makefile("rb") as stream:
            pending = bytes(buffer)
            while True:
                line, separator, pending = pending.partition(b"\n")
                if not separator:
                    rest = stream.readline(CONTROL_LIMIT)
                    if not rest:
                        break
                    line += rest
                kind, data = json.loads(line)
                if kind == "status":
                    return data
                if kind != "diag":
                    raise ValueError("Invalid cleanup result")
                pipeline_stderr(base64.b64decode(data))
        raise ConnectionError("SSH setup owner exited before reporting cleanup")


def staging_directories(directories):
    for directory, entries in directories.items():
        path = Path(directory)
        path.mkdir(mode=0o700)
        for name, value in entries.items():
            with (path / name).open("xb") as file:
                file.write(value.encode())
