import argparse
import base64
import inspect
import json
import os
import re
import secrets
import shlex
import socket
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

from .config import plan, read_rclone_config
from .copy import ARCHIVE_WORKERS, COPY_FAILED, INPUT_CLOSED, copy_plan, send_archive, send_input
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
STAGE = """import base64, json, os, sys
from pathlib import Path
os.umask(0o077)
for directory, entries in json.load(sys.stdin).items():
    path = Path(directory)
    path.mkdir(mode=0o700)
    for name, encoded in entries.items():
        with (path / name).open("xb") as file:
            file.write(base64.b64decode(encoded))
"""


def upload(ssh, target, result, sources=(), targets=(), modes=None):
    helper = files("oncolab").joinpath("copy.py").read_text()
    channel = [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target]
    args = [json.dumps(targets)]
    if modes is not None:
        args.append(json.dumps(modes))
    command = shlex.join(["python3", "-c", helper, result, *args])
    try:
        with TemporaryFile() as diagnostic, sp.Popen(
            [*channel, command], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=diagnostic,
        ) as process:
            try:
                with process.stdin:
                    send_archive(process.stdin, sources)
            except (OSError, ValueError) as exc:
                local_error = exc
            else:
                local_error = None
            status = process.wait()
            diagnostic.seek(0)
            detail = diagnostic.read().decode(errors="replace").strip()
        if not status and not local_error:
            return None, True
        # A closed pipe is usually a symptom of the receiver's error.
        if isinstance(local_error, BrokenPipeError) and detail:
            local_error = None
        error = (str(local_error) if local_error is not None else detail) or f"copy channel exited with status {status}"
        if status in (0, COPY_FAILED):
            return error, True
    except OSError as exc:
        error = str(exc)
    # If the receiver never launched, wake the remote runner as a failure.
    mark = shlex.join(["python3", "-c", helper, result, "--failed"])
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


def upload_stream(endpoint, key, source):
    # Unlike an SSH exec channel, each streamlocal forward is not counted
    # against MaxSessions. Keep data off the control channel and payload stdin.
    local_error = None
    try:
        with socket.socket(socket.AF_UNIX) as link:
            link.connect(str(endpoint))
            link.sendall(key.encode("ascii") + b"\n")
            try:
                with link.makefile("wb") as stream:
                    send_input(stream, source)
            except (OSError, ValueError) as exc:
                local_error = exc
            try:
                link.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            reply = link.recv(1)
    except OSError as exc:
        return f"could not open task stdin for {source.name}: {exc}", False
    status = reply[0] if reply else None
    if status == 0 and local_error is None:
        return None, True
    if status == INPUT_CLOSED and (local_error is None or isinstance(local_error, BrokenPipeError)):
        return None, True
    notified = status in (0, COPY_FAILED, INPUT_CLOSED)
    detail = str(local_error) if local_error else ("stream interrupted" if notified else "connection lost")
    return f"could not stream {source.name} to task stdin: {detail}", notified


def upload_tasks(ssh, target, jobdir, groups, endpoint=None):
    channel = [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target]
    helper = files("oncolab").joinpath("copy.py").read_text()
    types = {key: source is not None for key, (_, source) in groups.items()}
    command = shlex.join(["python3", "-c", helper, "--broker", jobdir, json.dumps(types)])
    jobs, errors = [], []
    with (
        ThreadPoolExecutor(max_workers=ARCHIVE_WORKERS) as archives,
        ThreadPoolExecutor(max_workers=max(1, sum(types.values()))) as streams,
        TemporaryFile() as diagnostic,
        sp.Popen([*channel, command], stdin=sp.DEVNULL, stdout=sp.PIPE,
                 stderr=diagnostic, text=True) as broker,
    ):
        def wake_runner(job):
            if job.cancelled():
                return
            if job.exception() is not None or not job.result()[1]:
                # Dropping only the broker wakes the remote copy waiters;
                # leave the command SSH channel alive for resource teardown.
                if broker.poll() is None:
                    broker.terminate()

        for line in broker.stdout:
            key = line.rstrip("\n")
            if key not in groups:
                continue
            entries, input_source = groups.pop(key)
            if input_source is None:
                sources, targets, modes = zip(*entries)
                job = archives.submit(upload, ssh, target, f"{jobdir}/{key}/copy-result", sources, targets, modes)
            else:
                job = streams.submit(upload_stream, endpoint, key, input_source)
            jobs.append(job)
            job.add_done_callback(wake_runner)
        if broker.wait():
            diagnostic.seek(0)
            errors.append(diagnostic.read().decode(errors="replace").strip() or "task copy channel disconnected")
            for job in jobs:
                job.cancel()
        for job in jobs:
            if job.cancelled():
                continue
            error, _ = job.result()
            if error:
                errors.append(error)
    return errors


def prepare_copies(config, specs, explicit):
    mounts = [Path(mount["target"]) for mount in config["mounts"].values()]
    workdir = config["workdir"]
    legacy_input = [*config.pop("copies", ()), *explicit]
    legacy = copy_plan(specs, workdir, mounts, explicit=legacy_input)
    task_groups = {}
    targets = [Path(target) for _, target in legacy]
    for name, step in config.get("setup", {}).items():
        entries = step.pop("copies", ())
        group = []
        for local, target, mode in entries:
            if target == "-":
                source = Path(local)
                if source.is_symlink() or not source.is_file():
                    raise ValueError(f"setup.{name}.copy: stdin source must be a regular file: {local}")
                group.append((source.resolve(), "-", 0o600))
                step["input"] = True
                continue
            [(source, destination)] = copy_plan([], workdir, mounts, explicit=[(local, target)])
            dest = Path(destination)
            if Path(workdir).is_relative_to(dest):
                raise ValueError(f"setup.{name}.copy: destination cannot contain workdir: {destination}")
            if any(dest.is_relative_to(other) or other.is_relative_to(dest) for other in targets):
                raise ValueError(f"Overlapping --copy destination: {destination}")
            targets.append(dest)
            group.append((source, destination, mode))
        if group:
            task_groups[name] = group
    return legacy, task_groups


def run_session(name, config, rclone_ini, known_hosts, command, forward, terminal, color, attempted, *,
                copies=(), task_copies=None):
    proxy, identity, public = ssh_options(name)
    target = "root@colab-runtime"
    drive_stage = "/dev/shm/oncolab-" + secrets.token_hex(12)
    remote = inspect.getsource(progress) + "\n" + files("oncolab").joinpath("remote.py").read_text()
    jobdir = drive_stage + "-job"
    result = jobdir + "/copy-result"
    config = dict(config)
    env = config.pop("env", {})
    command_env = config.pop("command_env", {})
    ports = config.pop("ports", ())
    task_copies = task_copies or {}
    groups = {}
    config["setup"] = {name: dict(step) for name, step in config["setup"].items()}
    markers = {"copy-result": "", "copy-ok": "ok", "copy-failed": "failed"}
    sources, targets = zip(*copies) if copies else ((), ())
    if env or command_env or sources or task_copies:
        config["jobdir"] = jobdir
    if sources:
        config["copy_result"] = result
        config["copy_supplies_workdir"] = any(Path(config["workdir"]).is_relative_to(path) for path in targets)
    staging = {}
    if rclone_ini is not None:
        staging[drive_stage] = {"rclone.conf": rclone_ini}
    if env or command_env or sources or task_copies:
        staging[jobdir] = {"env.json": json.dumps([env, command_env])} if env or command_env else {}
    if sources:
        staging[jobdir].update(markers)
    for number, (task, entries) in enumerate(task_copies.items()):
        key = f"task-{number}"
        copies_in_task = [entry for entry in entries if entry[1] != "-"]
        if copies_in_task:
            directory = f"{jobdir}/{key}"
            staging[directory] = markers
            config["setup"][task]["copy_result"] = f"{directory}/copy-result"
            groups[key] = (copies_in_task, None)
        source = next((source for source, destination, _ in entries if destination == "-"), None)
        config["setup"][task].pop("input", None)
        if source is not None:
            key += "-input"
            directory = f"{jobdir}/{key}"
            staging[directory] = {**markers, "copy-short": "short"}
            config["setup"][task]["stdin_result"] = f"{directory}/copy-result"
            groups[key] = ((), source)
    # The bootstrap channel must never read the command's stdin.
    staged_input = json.dumps({
        directory: {name: base64.b64encode(value.encode() if isinstance(value, str) else value).decode("ascii")
                    for name, value in entries.items()}
        for directory, entries in staging.items()
    }).encode() if staging else b""
    payload = shlex.join(["uv", "run", "--no-project", "python3", "-c", remote, drive_stage,
                          str(int(color)), json.dumps(config), *command])
    trust = (["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
             if known_hosts else ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}"])
    with TemporaryDirectory(prefix="oc-", dir="/tmp") as tmp, ThreadPoolExecutor(2) as transfers:
        ssh = ["ssh", "-F", os.devnull, "-S", f"{tmp}/ssh", "-o", "LogLevel=ERROR", "-A" if forward else "-a"]
        connect_options = [*identity, "-M", "-T", "-o", "ControlPersist=60",
                           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                           "-o", "ProxyCommand=" + shlex.join(proxy), *trust]
        endpoint = f"{tmp}/stdin.sock" if any(source is not None for _, source in groups.values()) else None
        if ports or endpoint:
            connect_options.extend(("-o", "ExitOnForwardFailure=yes"))
        if endpoint:
            connect_options.extend(("-L", f"{endpoint}:{jobdir}/stdin.sock"))
        for local, remote_port in ports:
            connect_options.extend(("-L", f"127.0.0.1:{local}:127.0.0.1:{remote_port}"))
        terminal_connect = False
        if terminal and public:
            keys = sp.run(["ssh-add", "-L"], capture_output=True).stdout.splitlines()
            advertised = public.read_bytes().split()[:2]
            terminal_connect = any(key.split()[:2] == advertised for key in keys)
        recover_pending = rclone_ini is not None
        master_closed = False

        def close_master():
            nonlocal master_closed
            if master_closed:
                return
            master_closed = True
            try:
                if recover_pending:
                    # The runner claims the pending file by rename. Never
                    # remove active credentials or disturb a running mount.
                    command = shlex.join(["rm", "-f", "--", f"{drive_stage}/rclone.conf"])
                    try:
                        cleanup = sp.run(
                            [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target, command],
                            stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=5,
                        )
                        confirmed = cleanup.returncode == 0
                    except (OSError, sp.TimeoutExpired):
                        confirmed = False
                    if not confirmed:
                        print(f"oncolab: could not confirm cleanup of unclaimed rclone config for {name!r}",
                              file=sys.stderr)
            finally:
                sp.run([*ssh, "-O", "exit", target], stdout=sp.DEVNULL, stderr=sp.DEVNULL)

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
            task_upload = transfers.submit(upload_tasks, ssh, target, jobdir, groups, endpoint) if groups else None
            argv = [*ssh, "-o", "ProxyCommand=false", "-o", "ControlMaster=no", tty, target, payload]
            status = call_with_upload(argv, transfer) if transfer else sp.call(argv)
            if status == 0:
                recover_pending = False
            if status and ((transfer and not transfer.done()) or (task_upload and not task_upload.done())):
                close_master()
            if transfer:
                error, _ = transfer.result()
                if error:
                    print(f"oncolab: {error}", file=sys.stderr)
                    status = status or 1
            if task_upload:
                try:
                    transfer_errors = task_upload.result(timeout=3)
                except TimeoutError:
                    # The payload has exited; no remote teardown can still be waiting on this broker.
                    close_master()
                    transfer_errors = task_upload.result()
                for error in transfer_errors:
                    print(f"oncolab: {error}", file=sys.stderr)
                    status = status or 1
            return status, False
        finally:
            close_master()


def main():
    parser = argparse.ArgumentParser(description="Run a terminal command on a fresh Google Colab VM.")
    parser.add_argument("--version", action="version", version=f"oncolab {version('oncolab')}")
    parser.add_argument("--copy", action="append", default=[], metavar="LOCAL[:REMOTE]",
                        help="Copy a file or directory to the VM before running; repeatable")
    parser.add_argument("--copy-to", action="append", nargs=2, default=[], metavar=("LOCAL", "REMOTE"),
                        help="Copy using separate literal paths; repeatable")
    parser.add_argument("--mount", action="append", default=[], metavar="source=REMOTE:PATH,target=PATH",
                        help="Mount an rclone remote path; repeatable")
    parser.add_argument("-p", "--publish", action="append", default=[], metavar="LOCAL_PORT:REMOTE_PORT",
                        help="Forward a local loopback TCP port to the VM; repeatable")
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
        config, rclone_path, known_hosts = plan(args.config, copy_requested=bool(args.copy or args.copy_to),
                                            cli_mounts=args.mount, cli_ports=args.publish)
        config["command_env"] = parse_env(args.env)
        copies, task_groups = prepare_copies(config, args.copy, args.copy_to)
        ini = read_rclone_config(rclone_path) if rclone_path is not None else None
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
                                       args.forward_agent, terminal, color, attempted, copies=copies,
                                       **({"task_copies": task_groups} if task_groups else {}))
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
