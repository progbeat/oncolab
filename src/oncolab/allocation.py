"""Own the allocation subprocess, progress, and cancellation without owning SSH."""
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess as sp
import sys
from contextlib import nullcontext
from importlib.resources import files
from pathlib import Path
from signal import SIGINT
from tempfile import TemporaryFile
from time import monotonic

from .diagnostics import safe_subprocess_details
from .progress import CHECKING_LOGIN, WAITING, run_step

CHECK = "Check 'colab sessions' for an unconfirmed VM."


class AllocationError(Exception):
    pass


class Cancellation:
    """Wake the supervisor immediately, including at the end of backoff."""
    def __init__(self):
        self.requested = False
        self.read, self.write = os.pipe()

    def request(self, *_):
        if not self.requested:
            self.requested = True
            # Exactly one byte is written during this allocation.
            os.write(self.write, b"!")

    def close(self):
        os.close(self.read)
        os.close(self.write)


class AllocationLabel:
    state = (CHECKING_LOGIN, "", None)

    def __call__(self):
        return self.state[0]

    def detail(self):
        _, detail, deadline = self.state
        if deadline is None:
            return detail
        from math import ceil
        return f"{max(0, ceil(deadline - monotonic()))}s · {detail}"


def colab_python():
    """Use the interpreter of the uv-installed Colab launcher, not ours."""
    colab = shutil.which("colab")
    if not colab:
        raise AllocationError("Colab CLI not found. Run 'uv tool install google-colab-cli'.")
    with open(colab, "rb") as script:
        lines = [script.readline(4096).decode("utf-8", "replace").strip() for _ in range(3)]
    if lines[0].startswith("#!"):
        command = shlex.split(lines[0][2:])
        if command in (["/bin/sh"], ["/bin/bash"]) and lines[1].startswith("'''exec' "):
            # uv uses a shell trampoline for interpreter paths containing spaces.
            command = shlex.split(lines[1][len("'''exec' "):].split(' "$0" "$@"', 1)[0])
        elif command[:2] == ["/usr/bin/env", "-S"]:
            command = [command[0], *command[2:]]
        if command and re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?t?)?", Path(command[-1]).name):
            return command
    raise AllocationError("Cannot identify the Colab CLI's Python. Install it with 'uv tool install google-colab-cli'.")


def write_diagnostic(message, *, optional=False):
    try:
        from io import UnsupportedOperation
        try:
            fd = sys.stderr.fileno()
        except (AttributeError, UnsupportedOperation):
            print(message, file=sys.stderr, flush=True)
            return
        data = (message + "\n").encode()
        if optional:
            from select import select
            if not select([], [fd], [], 0)[1]:
                return
            if len(data) > 512:
                data = data[:511] + b"\n"
        os.write(fd, data)
    except (OSError, ValueError):
        # The user may close a diagnostic pipeline while the worker is alive.
        pass


def launch(argv, label, cancellation, diagnostics, *, debug, live, budget):
    if cancellation.requested:
        return 130
    debug_stream = TemporaryFile() if debug else nullcontext(None)
    with debug_stream as error_stream:
        with sp.Popen(argv, stdin=sp.DEVNULL, stdout=sp.PIPE,
                      stderr=error_stream if error_stream is not None else sp.DEVNULL,
                      start_new_session=True, bufsize=0) as proc:
            try:
                return supervise(proc, label, cancellation, diagnostics, debug=debug, live=live, budget=budget)
            finally:
                if proc.poll() is None:
                    # Do not let an unexpected error in the monitor turn Popen's
                    # context-manager wait into an unbounded wait on an allocator.
                    proc.send_signal(SIGINT)
                    try:
                        proc.wait(timeout=11)
                    except sp.TimeoutExpired:
                        proc.kill()
                        diagnostics.append(f"oncolab: {CHECK}")
                if error_stream is not None:
                    # Native launcher/import failures happen before the JSON
                    # protocol. Keep only a bounded tail, never raw stderr.
                    end = error_stream.seek(0, os.SEEK_END)
                    error_stream.seek(max(0, end - 16_384))
                    text = error_stream.read(16_384).decode(errors="replace")
                    diagnostics.extend(safe_subprocess_details(text))


def supervise(proc, label, cancellation, diagnostics, *, debug, live, budget):
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(cancellation.read, selectors.EVENT_READ)
        hard_deadline = monotonic() + budget
        interrupt_at = None
        explained = False
        data = b""
        while selector.get_map():
            now = monotonic()
            if (cancellation.requested or now >= hard_deadline) and interrupt_at is None:
                interrupt_at = now
                label.state = ("Cancelling allocation…", "checking Colab", None)
                proc.send_signal(SIGINT)
                if cancellation.read in selector.get_map():
                    selector.unregister(cancellation.read)
            if interrupt_at is not None and now - interrupt_at >= 11:
                proc.kill()
                if cancellation.requested:
                    diagnostics.append(f"oncolab: {CHECK}")
                    return 130
                diagnostics.append(f"oncolab: Timed out waiting for Colab. {CHECK}")
                return 124
            deadline = hard_deadline if interrupt_at is None else interrupt_at + 11
            ready = selector.select(max(0, deadline - monotonic()))
            if not ready:
                continue
            if any(key.fd == cancellation.read for key, _ in ready):
                # Process cancellation before any label events already queued.
                continue
            block = os.read(proc.stdout.fileno(), 4096)
            if not block:
                selector.unregister(proc.stdout)
                break
            data += block
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if message := event.get("label"):
                    deadline = event.get("deadline")
                    label.state = (message, event.get("detail", ""), deadline)
                    hard_deadline = (deadline or monotonic()) + budget
                    if not live and message == WAITING:
                        write_diagnostic(f"oncolab: {message} {label.detail()}", optional=True)
                if message := event.get("error"):
                    explained = True
                    diagnostics.append(f"oncolab: {message}")
                if debug and (message := event.get("debug")):
                    diagnostics.append(message)
        try:
            status = proc.wait(timeout=1)
        except sp.TimeoutExpired:
            proc.kill()
            diagnostics.append(f"oncolab: {CHECK}")
            return 130 if cancellation.requested else 1
        if interrupt_at is not None and not cancellation.requested:
            diagnostics.append("oncolab: Timed out waiting for Colab. " + CHECK)
            return 124
        elif status < 0 and cancellation.requested:
            diagnostics.append(f"oncolab: {CHECK}")
        elif status and not (cancellation.requested or explained):
            diagnostics.append(f"oncolab: The Colab allocator exited unexpectedly (status {status}). {CHECK}")
        return status if status >= 0 else (130 if cancellation.requested else 1)


def run_allocator(args, store, name, terminal, color, cancellation, labels):
    script = str(files("oncolab").joinpath("allocation_worker.py"))
    accelerator = args.gpu or args.tpu or "NONE"
    variant = "GPU" if args.gpu else "TPU" if args.tpu else "DEFAULT"
    argv = [*colab_python(), "-I", script, str(store), name, accelerator, variant,
            str(int(args.high_mem)), str(args.attempts), str(int(args.debug)),
            str(args.request_timeout), str(args.retry_backoff)]
    # One attempt can perform both a lookup and an allocation POST.
    budget = max(45, 2 * args.request_timeout + 5)
    label = AllocationLabel()
    diagnostics = []
    try:
        status = run_step(label, lambda: launch(argv, label, cancellation, diagnostics,
                                                debug=args.debug, live=terminal, budget=budget),
                          terminal, color, labels=labels)
    finally:
        # Do not write diagnostics over the live prompt-toolkit renderer.
        for message in diagnostics:
            # A signal can arrive between this call and os.write. Never
            # block here while the allocation handler is still installed.
            write_diagnostic(message, optional=True)
    if status == 0 and not store.is_file():
        raise AllocationError("The Colab allocator did not store the new session. " + CHECK)
    return status
