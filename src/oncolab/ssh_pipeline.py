"""Private control socket for a setup owner and a native SSH command channel."""

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import select
import signal as signals
import subprocess as sp
import sys
import time


SETUP_LISTENING = b"oncolab-setup-listening\n"
SETUP_CANCEL = b'["cancelled",null]\n'
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
        self.peer = None
        self.pending = []

    async def accept(self):
        loop = asyncio.get_running_loop()
        self.peer, _ = await asyncio.wait_for(loop.sock_accept(self.listener), 20)
        self.peer.setblocking(True)
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
        if self.peer is not None:
            self.peer.close()

    def receive_status(self, interrupted):
        buffer = bytearray()
        child = None
        exited = False
        interactive_admitted = False
        cancel_at = None
        try:
            while True:
                if interrupted() and cancel_at is None:
                    cancel_at = time.monotonic()
                    self.send("cancel")
                if cancel_at is not None and time.monotonic() - cancel_at >= 4:
                    raise ConnectionError("SSH command did not acknowledge cancellation")
                data = receive(self.peer, buffer, .1)
                if data is None:
                    continue
                kind, value = data
                if kind == "cancelled" and child is None:
                    return 130
                if kind == "started" and child is None:
                    pid, interactive = value
                    if type(interactive) is not bool:
                        raise ValueError("Invalid command terminal state")
                    child = CommandHandle(pid)
                    interactive_admitted = interactive and cancel_at is None
                    if cancel_at is None:
                        self.send("go")
                elif kind == "exit" and type(value) is int and 0 <= value <= 255:
                    exited = True
                    return (128 + interrupted()) if interrupted() else value
                elif kind == "unconfirmed":
                    raise CommandStateUnknown("Could not confirm that the command stopped")
                else:
                    raise ValueError("Invalid command status")
        finally:
            if child is not None:
                try:
                    if not exited:
                        child.stop()
                        if interactive_admitted:
                            # Killing the shell's group does not establish
                            # that other terminal job-control groups stopped.
                            raise CommandStateUnknown("Could not confirm that the interactive command stopped")
                finally:
                    child.close()


class CommandStateUnknown(RuntimeError):
    """Do not release mounts or the VM while a command might still be using them."""


class CommandHandle:
    def __init__(self, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError("Invalid command process")
        self.pid = pid
        self.fd = None
        self.gone = False
        try:
            self.fd = os.pidfd_open(pid)
        except ProcessLookupError:
            self.gone = True
        except (AttributeError, OSError):
            # Non-Linux tests or a kernel lacking pidfds: don't guess about
            # process identity if the supervising channel disappears.
            pass

    def stop(self):
        if self.gone:
            return
        if self.fd is None:
            raise CommandStateUnknown("Could not confirm that the command stopped")
        try:
            # The admitted process is its own group leader. Stop its
            # descendants in that group as well, not just the leader.
            signal_command_group(self.pid, signals.SIGTERM)
            if not select.select([self.fd], [], [], 1)[0]:
                signal_command_group(self.pid, signals.SIGKILL)
            else:
                # The leader may exit before children that ignored TERM.
                signal_command_group(self.pid, signals.SIGKILL)
            if select.select([self.fd], [], [], 2)[0]:
                return
        except OSError as exc:
            raise CommandStateUnknown("Could not confirm that the command stopped") from exc
        raise CommandStateUnknown("Could not confirm that the command stopped")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


def receive(peer, buffer, timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    while b"\n" not in buffer:
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        if not select.select([peer], [], [], remaining)[0]:
            return None
        block = peer.recv(65536)
        if not block:
            raise ConnectionError("SSH setup control socket closed")
        buffer.extend(block)
        if len(buffer) > CONTROL_LIMIT:
            raise ValueError("SSH setup message too large")
    i = buffer.index(b"\n")
    line = bytes(buffer[:i])
    del buffer[:i+1]
    return json.loads(line)


async def observe_setup(run, peer, buffer, live):
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
                if live:
                    output.append(fields[0])
                else:
                    pipeline_stderr(base64.b64decode(fields[0]))
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


def receive_cleanup(peer, buffer, *, cancelling=False):
    while True:
        kind, *fields = receive(peer, buffer)
        if kind == "status":
            return fields[0]
        if kind == "diag":
            pipeline_stderr(base64.b64decode(fields[0]))
        elif kind == "end-diagnostics":
            peer.sendall(packet("drained"))
        elif not cancelling:
            raise ValueError("Invalid cleanup result")


COMMAND_ENTRY = r"""
import json, os, signal, sys
with os.fdopen(int(sys.argv[1]), "rb") as admission:
    raw = admission.read()
if not raw:
    raise SystemExit(130)
cwd, env, color = json.loads(raw)
args = sys.argv[2:]
try:
    os.chdir(cwd)
    # This Python trampoline reinstalls ignored signals after Popen resets
    # them. Restore the dispositions a direct Popen gives the user's command.
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if (signum := getattr(signal, name, None)) is not None:
            signal.signal(signum, signal.SIG_DFL)
    os.execvpe(args[0], args, env)
except OSError as exc:
    heading = "[Command could not start]"
    if color and sys.stderr.isatty():
        heading = "\x1b[31m" + heading + "\x1b[0m"
    print(f"{heading} {args[0]}: {exc.strerror}.", file=sys.stderr)
    raise SystemExit(127 if isinstance(exc, FileNotFoundError) else 126) from None
"""


def signal_command_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def stop_gate_command(child):
    signal_command_group(child.pid, signals.SIGTERM)
    try:
        child.wait(timeout=1)
    except sp.TimeoutExpired:
        pass
    # Even if the group leader exited, a child could still have ignored TERM.
    signal_command_group(child.pid, signals.SIGKILL)
    try:
        child.wait(timeout=2)
    except sp.TimeoutExpired as exc:
        raise CommandStateUnknown("Could not confirm that the command stopped") from exc


def foreground_group(fd, group):
    handler = signals.signal(signals.SIGTTOU, signals.SIG_IGN)
    try:
        os.tcsetpgrp(fd, group)
    finally:
        signals.signal(signals.SIGTTOU, handler)


def gated_command(peer, buffer, workdir, environment, command, *, color=False):
    read_fd, write_fd = os.pipe()
    child = None
    terminal = None
    old_group = None
    finished = False
    try:
        # Python and its loader must not see user variables or cwd until
        # admission; PYTHONHOME can otherwise break non-Python commands.
        child = sp.Popen([sys.executable, "-I", "-S", "-c", COMMAND_ENTRY, str(read_fd), *command],
                         cwd="/", env={"PATH": os.defpath}, pass_fds=(read_fd,), process_group=0)
        os.close(read_fd)
        read_fd = -1
        peer.sendall(packet("started", [child.pid, sys.stdin.isatty()]))
        reply = receive(peer, buffer)
        if reply == ["cancel"]:
            return 130
        if reply != ["go"]:
            raise ValueError("SSH setup owner did not admit the command")
        if sys.stdin.isatty():
            terminal = sys.stdin.fileno()
            try:
                old_group = os.tcgetpgrp(terminal)
                foreground_group(terminal, child.pid)
            except OSError:
                old_group = None
        admission = os.fdopen(write_fd, "wb")
        write_fd = -1
        with admission:
            admission.write(json.dumps([workdir, environment, color]).encode())
        while child.poll() is None:
            try:
                event = receive(peer, buffer, .1)
                if event == ["cancel"]:
                    return 130
                if event is not None:
                    raise ValueError("Invalid command control request")
            except KeyboardInterrupt:
                # A native PTY delivers Ctrl+C to the command itself too.
                continue
        finished = True
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        try:
            if child is not None and not finished:
                stop_gate_command(child)
                if old_group is not None:
                    # Interactive shells can move jobs into other process
                    # groups. Do not tear down mounts on an external abort
                    # unless we can prove the whole terminal session is idle.
                    raise CommandStateUnknown("Could not confirm that the interactive command stopped")
        finally:
            if old_group is not None:
                try:
                    foreground_group(terminal, old_group)
                except OSError:
                    pass


def command_gate(path, plan, color, command, render, takeover):
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

        if not live:
            takeover()
        try:
            output, result = render(lambda run: observe_setup(run, peer, buffer, live), live, color,
                                    labels=plan.get("progress_labels", ()),
                                    idle=plan.get("terminal_handoff"), takeover=lambda: takeover(erase=False))
        except KeyboardInterrupt:
            takeover()
            pipeline_stderr(b"[cancelled]\n")
            peer.setblocking(True)
            peer.sendall(SETUP_CANCEL)
            return receive_cleanup(peer, buffer, cancelling=True)
        peer.setblocking(True)
        for data in output:
            pipeline_stderr(base64.b64decode(data))
        kind, *args = result
        if kind == "run":
            workdir, env = args
            environment = {k: v for k, v in (os.environ | env).items() if v is not None}
            try:
                status = gated_command(peer, buffer, workdir, environment, command, color=color and live)
            except CommandStateUnknown:
                peer.sendall(packet("unconfirmed", None))
            else:
                peer.sendall(packet("exit", status))
        elif kind != "failed":
            raise ValueError("Invalid setup result")
        # Keep the channel alive until the setup owner has completed cleanup.
        return receive_cleanup(peer, buffer)


def command_gate_main(render):
    path, color, raw_plan, *command = sys.argv[1:]
    plan = json.loads(raw_plan)
    handoff = bool(plan.get("terminal_handoff"))

    def takeover(*, erase=True):
        nonlocal handoff
        if handoff:
            handoff = False
            print("\x1b[A\r" + ("\x1b[K" if erase else ""), end="", flush=True)

    try:
        return command_gate(path, plan, color == "1", command, render, takeover)
    except KeyboardInterrupt:
        takeover()
        pipeline_stderr(b"[cancelled]\n")
        return 130
    except (OSError, ValueError):
        takeover()
        hint = plan.get("recovery_hint", "Setup runner disconnected")
        pipeline_stderr(f"[Setup channel closed] {hint}.\n".encode())
        return 1


def staging_directories(directories):
    for directory, entries in directories.items():
        path = Path(directory)
        path.mkdir(mode=0o700)
        for name, value in entries.items():
            with (path / name).open("xb") as file:
                file.write(value.encode())
