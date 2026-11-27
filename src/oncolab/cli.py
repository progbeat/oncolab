import argparse
import inspect
import json
import os
import re
import secrets
import shlex
import subprocess as sp
import sys
import termios
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from signal import SIGINT, SIG_IGN, signal
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event

from .config import credentials, plan
from .copy import copy_plan, send_archive
from .env import parse_env
from .progress import progress, run_step


@contextmanager
def without_echo(enabled):
    if not (enabled and sys.stdin.isatty()):
        yield False
        return
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    quiet = original.copy()
    quiet[3] &= ~(termios.ECHO | termios.ECHONL)
    termios.tcsetattr(fd, termios.TCSANOW, quiet)
    try:
        yield bool(original[3] & termios.ECHO)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)


def release(name, terminal, color):
    with without_echo(terminal):
        result = run_step("Releasing Colab VM…", lambda: sp.run(
            ["colab", "stop", "-s", name], capture_output=True, stdin=sp.DEVNULL,
        ), terminal, color)
    if result.returncode:
        print(f"oncolab: could not confirm release of {name!r}; check colab sessions", file=sys.stderr)


def allocate(flags, terminal, color):
    cancelled = False

    def cancel(*_):
        nonlocal cancelled
        if not cancelled and not terminal:
            print("Cancelling: waiting for Colab…", file=sys.stderr, flush=True)
        cancelled = True

    def label():
        return "Cancelling: waiting for Colab…" if cancelled else "Creating Colab VM…"

    previous = signal(SIGINT, cancel)
    try:
        with without_echo(terminal) as echo_was_on:
            result = run_step(label, lambda: sp.run(
                ["colab", "new", *flags], capture_output=True, start_new_session=True,
            ), terminal, color)
    finally:
        signal(SIGINT, previous)
    match = re.search(rb"^\[colab\] Creating session '([A-Za-z0-9_-]+)'", result.stdout, re.MULTILINE)
    name = match[1].decode() if match and not result.returncode else None
    if cancelled:
        signal(SIGINT, SIG_IGN)
        try:
            if name:
                release(name, terminal, color)
            else:
                print("oncolab: could not confirm allocation; check colab sessions", file=sys.stderr)
            cancel_footer(echo_was_on, color)
        finally:
            signal(SIGINT, previous)
        return None, 130
    if name:
        return name, 0
    error = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", result.stderr)
    if allocation_unavailable(error):
        print("oncolab: Google Colab could not allocate a VM (503 Service Unavailable). "
              "Try again in a moment.", file=sys.stderr)
    else:
        sys.stderr.buffer.write(result.stdout + result.stderr)
    return None, result.returncode or 1



def allocation_unavailable(stderr):
    # Rich may wrap the URL in the final exception across terminal lines.
    _, found, message = stderr.rpartition(b"ColabRequestError:")
    return bool(found and re.fullmatch(
        rb"\s*Failed to issue request POST\s+"
        rb"https://colab\.research\.google\.com/tun/m/assign\?[^:]*:"
        rb"\s*Service Unavailable\s*",
        message,
    ))

def cancel_footer(echo, color):
    tty = sys.stderr.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    prefix = "^C\n" if echo and tty else ""
    if tty and color:
        from prompt_toolkit import print_formatted_text
        from prompt_toolkit.formatted_text import FormattedText

        print_formatted_text(FormattedText([("", prefix), ("class:aborting", "[cancelled]")]),
                             file=sys.stderr, flush=True)
    else:
        print(prefix + "[cancelled]", file=sys.stderr, flush=True)


def ssh_options(name):
    proxy = ["colab", "ssh", "--proxy-mode", "-s", name]
    for filename in ("id_ed25519.pub", "id_ecdsa.pub"):
        public = Path.home() / ".ssh" / filename
        if public.is_file():
            proxy += ["--identity", str(public)]
            private = public.with_suffix("")
            return proxy, ["-o", "IdentitiesOnly=yes", "-i", str(private if private.is_file() else public)], public
    return proxy, [], None


# File contents and runtime environment values travel via SSH stdin, not argv.
STAGE = """import json, os, sys
from pathlib import Path
os.umask(0o077)
for directory, entries in json.load(sys.stdin).items():
    path = Path(directory)
    path.mkdir(mode=0o700)
    for name, text in entries.items():
        with (path / name).open("x") as file:
            file.write(text)
"""


def upload(ssh, target, result, sources, targets):
    helper = files("oncolab").joinpath("copy.py").read_text()
    channel = [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target]
    command = shlex.join(["python3", "-c", helper, result, json.dumps(targets)])
    try:
        with TemporaryFile() as diagnostic, sp.Popen(
            [*channel, command], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=diagnostic,
        ) as process:
            try:
                with process.stdin:
                    send_archive(process.stdin, sources)
            except (OSError, ValueError) as exc:
                local_error = str(exc)
            else:
                local_error = ""
            status = process.wait()
            diagnostic.seek(0)
            detail = diagnostic.read().decode(errors="replace").strip()
        if not status and not local_error:
            return None, True
        error = local_error or detail or f"copy channel exited with status {status}"
    except OSError as exc:
        error = str(exc)
    # If the receiver never launched, wake the remote runner as a failure.
    mark = f"test -f {shlex.quote(result)} || printf failed > {shlex.quote(result)}"
    notified = sp.run([*channel, mark], stdin=sp.DEVNULL, stdout=sp.DEVNULL,
                      stderr=sp.DEVNULL).returncode == 0
    return error, notified


def call_with_upload(argv, transfer):
    process = sp.Popen(argv)
    try:
        while not transfer.done():
            try:
                status = process.wait(timeout=.2)
            except sp.TimeoutExpired:
                continue
            return status
        _, notified = transfer.result()
        # Otherwise the remote runner could wait for a marker that cannot arrive.
        if not notified and process.poll() is None:
            return 255
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def run_session(name, config, rclone_ini, known_hosts, command, forward, terminal, color, attempted, *, sources=()):
    proxy, identity, public = ssh_options(name)
    target = "root@colab-runtime"
    workdir = "/dev/shm/oncolab-" + secrets.token_hex(12)
    remote = inspect.getsource(progress) + "\n" + files("oncolab").joinpath("remote.py").read_text()
    jobdir = workdir + "-job"
    result = jobdir + "/copy-result"
    config = dict(config)
    env = config.pop("env", {})
    targets = config.pop("copy_targets", [])
    if env or sources:
        config["jobdir"] = jobdir
    if sources:
        config["copy_result"] = result
    staging = {}
    if rclone_ini:
        staging[workdir] = {"rclone.conf": rclone_ini.decode()}
    if env or sources:
        staging[jobdir] = {"env.json": json.dumps(env)} if env else {}
    staged_input = json.dumps(staging).encode() if staging else None
    payload = shlex.join(["uv", "run", "--no-project", "python3", "-c", remote, workdir,
                          str(int(color)), json.dumps(config), *command])
    trust = (["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
             if known_hosts else ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}"])
    with TemporaryDirectory(prefix="oc-", dir="/tmp") as tmp, ThreadPoolExecutor(1) as transfers:
        ssh = ["ssh", "-F", os.devnull, "-S", f"{tmp}/ssh", "-o", "LogLevel=ERROR", "-A" if forward else "-a"]
        connect_options = [*identity, "-M", "-T", "-o", "ControlPersist=60",
                           "-o", "ProxyCommand=" + shlex.join(proxy), *trust]
        terminal_connect = False
        if terminal and public:
            keys = sp.run(["ssh-add", "-L"], capture_output=True).stdout.splitlines()
            advertised = public.read_bytes().split()[:2]
            terminal_connect = any(key.split()[:2] == advertised for key in keys)
        stage = shlex.join(["python3", "-c", STAGE]) if staging else "true"
        try:
            with without_echo(terminal_connect):
                connected = run_step("Connecting via SSH…", lambda: sp.run(
                    [*ssh, *connect_options, target, stage],
                    input=staged_input, capture_output=terminal_connect,
                ), terminal_connect, color)
            if terminal_connect:
                # Setup diagnostics must not mix with payload stdout.
                sys.stderr.buffer.write(connected.stderr)
                sys.stderr.flush()
            if connected.returncode:
                return connected.returncode, True
            tty = "-t" if sys.stdin.isatty() and sys.stdout.isatty() else "-T"
            attempted.set()
            transfer = transfers.submit(upload, ssh, target, result, sources, targets) if sources else None
            argv = [*ssh, "-o", "ProxyCommand=false", "-o", "ControlMaster=no", tty, target, payload]
            status = call_with_upload(argv, transfer) if transfer else sp.call(argv)
            if transfer:
                if status and not transfer.done():
                    sp.run([*ssh, "-O", "exit", target], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                error, _ = transfer.result()
                if error:
                    print(f"oncolab: {error}", file=sys.stderr)
                    status = status or 1
            return status, False
        finally:
            sp.run([*ssh, "-O", "exit", target], stdout=sp.DEVNULL, stderr=sp.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description="Run a terminal command on a fresh Google Colab VM.")
    parser.add_argument("--version", action="version", version=f"oncolab {version('oncolab')}")
    parser.add_argument("--copy", action="append", default=[], metavar="LOCAL[:REMOTE]",
                        help="Copy a file or directory to the VM before running; repeatable")
    parser.add_argument("-e", "--env", action="append", default=[], metavar="NAME[=VALUE]",
                        help="Set a value or forward a named local variable; repeatable")
    parser.add_argument("--config", type=Path, help="Use an explicit local TOML configuration")
    hardware = parser.add_mutually_exclusive_group()
    hardware.add_argument("--gpu", metavar="MODEL",
                          help="Request a GPU: T4, L4, G4, A100, H100 (subject to availability)")
    hardware.add_argument("--tpu", metavar="MODEL",
                          help="Request a TPU: V5E1, V6E1 (subject to availability)")
    parser.add_argument("--high-mem", action="store_true", help="Request a high-memory VM")
    parser.add_argument("-A", "--forward-agent", action="store_true", help="Forward the local SSH agent")
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="COMMAND")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    try:
        config, origins, known_hosts = plan(args.config, copy_requested=bool(args.copy))
        config["env"] |= parse_env(args.env)
        sources, targets = copy_plan(args.copy, config["workdir"],
                                     [Path(m["target"]) for m in config["mounts"].values()])
        config["copy_targets"] = targets
        ini = credentials(origins) if origins else b""
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    flags = ["--gpu", args.gpu] if args.gpu else ["--tpu", args.tpu] if args.tpu else []
    if args.high_mem:
        flags.append("--high-mem")
    color = not os.environ.get("NO_COLOR")
    terminal = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    name = None
    attempted = Event()
    try:
        name, status = allocate(flags, terminal, color)
        if not name:
            return status
        status, fallback = run_session(name, config, ini, known_hosts, command,
                                       args.forward_agent, terminal, color, attempted, sources=sources)
        if fallback:
            release(name, terminal, color)
        if attempted.is_set() and (status == 255 or status < 0):
            print(f"oncolab: SSH status is ambiguous; check Colab session {name!r}", file=sys.stderr)
        return status if status >= 0 else 128 - status
    except KeyboardInterrupt:
        # After allocation, an interrupted SSH connection may leave the mount
        # running. Do not force-stop a VM with potentially pending writes.
        if name and not attempted.is_set():
            signal(SIGINT, SIG_IGN)
            release(name, terminal, color)
        elif name:
            print(f"oncolab: check colab sessions for {name!r} if it is still running", file=sys.stderr)
        cancel_footer(False, color)
        return 130
    except OSError as exc:
        print(f"oncolab: {exc}", file=sys.stderr)
        if name and not attempted.is_set():
            release(name, terminal, color)
        return 1
