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

from .allocation import AllocationError, Cancellation, run_allocator
from .config import plan, read_rclone_config
from .copy import ARCHIVE_WORKERS, COPY_FAILED, INPUT_CLOSED, copy_plan, send_archive, send_input
from .env import parse_env
from .progress import CONNECTING, preparation_labels, progress, run_step


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


def colab_command(store):
    return ["colab", "--config", str(store)] if store is not None else ["colab"]


def allocation_hint(name, store):
    try:
        endpoint = json.loads(store.read_text())[name]["endpoint"] if store else None
    except (OSError, ValueError, KeyError, TypeError):
        endpoint = None
    if isinstance(endpoint, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", endpoint):
        return f"check 'colab sessions' for VM {endpoint!r}"
    return "check 'colab sessions' for the VM"


def release(name, terminal, color, store=None):
    hint = allocation_hint(name, store)
    with without_echo(terminal):
        try:
            result = run_step("Releasing Colab VM…", lambda: sp.run(
                [*colab_command(store), "stop", "-s", name], capture_output=True,
                stdin=sp.DEVNULL, timeout=12,
            ), terminal, color, timed=False)
            if result.returncode == 0 and b"Session terminated." in result.stdout:
                return
        except (OSError, sp.TimeoutExpired):
            pass
    print(f"oncolab: could not confirm release; {hint}", file=sys.stderr)


def allocate(args, terminal, color, store, labels):
    cancellation = Cancellation()
    name = "oncolab-" + secrets.token_hex(6)
    try:
        previous = signal(SIGINT, cancellation.request)
        try:
            with without_echo(terminal) as echo_was_on:
                status = run_allocator(args, store, name, terminal, color, cancellation, labels)
        finally:
            # Stop recording cancellation before choosing the outcome.
            # A subsequent Ctrl+C reaches execute's pre-SSH cleanup.
            signal(SIGINT, previous)
        if (cancellation.requested or status != 0) and store.is_file():
            cleanup_handler = signal(SIGINT, SIG_IGN)
            try:
                release(name, terminal, color, store)
            finally:
                signal(SIGINT, cleanup_handler)
        if cancellation.requested or status == 130:
            cancel_footer(echo_was_on, color)
            return None, 130
        return (name, 0) if status == 0 else (None, status)
    finally:
        cancellation.close()


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


def ssh_options(name, store=None):
    proxy = [*colab_command(store), "ssh", "--proxy-mode", "-s", name]
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
try:
    for directory, entries in json.load(sys.stdin).items():
        path = Path(directory)
        path.mkdir(mode=0o700)
        for name, encoded in entries.items():
            with (path / name).open("xb") as file:
                file.write(base64.b64decode(encoded))
except Exception as exc:
    print("oncolab-stage: " + type(exc).__name__, file=sys.stderr)
    raise SystemExit(1) from None
"""


def copy_detail(diagnostic):
    for line in diagnostic:
        if not line.startswith(b"oncolab-copy: "):
            continue
        try:
            message = json.loads(line.removeprefix(b"oncolab-copy: "))
        except ValueError:
            continue
        if isinstance(message, str):
            return "".join(c if c.isprintable() else " " for c in message)
    return ""


def copy_helper(debug):
    return f"_ONCOLAB_DEBUG = {debug!r}\n" + files("oncolab").joinpath("copy.py").read_text()


def upload(ssh, target, result, sources=(), targets=(), modes=None, *, debug=False):
    helper = copy_helper(debug)
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
            detail = copy_detail(diagnostic)
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


def upload_tasks(ssh, target, jobdir, groups, endpoint=None, *, debug=False):
    channel = [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target]
    helper = copy_helper(debug)
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
        def report_result(job):
            if job.cancelled():
                return
            if job.exception() is not None:
                notified = False
            else:
                error, notified = job.result()
                if error:
                    # Ready services can finish receiving stdin after the command starts.
                    # Report late failures now, without cutting off SSH or teardown.
                    print(f"oncolab: {error}", file=sys.stderr, flush=True)
            if not notified and broker.poll() is None:
                # Dropping only the broker wakes the remote copy waiters.
                broker.terminate()

        for line in broker.stdout:
            key = line.rstrip("\n")
            if key not in groups:
                continue
            entries, input_source = groups.pop(key)
            if input_source is None:
                sources, targets, modes = zip(*entries)
                job = archives.submit(upload, ssh, target, f"{jobdir}/{key}/copy-result",
                                      sources, targets, modes, debug=debug)
            else:
                job = streams.submit(upload_stream, endpoint, key, input_source)
            jobs.append(job)
            job.add_done_callback(report_result)
        if broker.wait():
            diagnostic.seek(0)
            error = copy_detail(diagnostic) or "task copy channel disconnected"
            print(f"oncolab: {error}", file=sys.stderr, flush=True)
            errors.append(error)
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
    cli_copies = copy_plan(specs, workdir, mounts, explicit=explicit)
    task_groups = {}
    targets = [Path(target) for _, target in cli_copies]
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
    return cli_copies, task_groups


def run_session(name, config, rclone_ini, known_hosts, command, forward, terminal, color, attempted, *,
                copies=(), task_copies=None, store=None, debug=False):
    proxy, identity, public = ssh_options(name, store)
    target = "root@colab-runtime"
    drive_stage = "/dev/shm/oncolab-" + secrets.token_hex(12)
    remote = (f"_ONCOLAB_DEBUG = {debug!r}\n" + inspect.getsource(progress) + "\n"
              + files("oncolab").joinpath("remote.py").read_text())
    jobdir = drive_stage + "-job"
    result = jobdir + "/copy-result"
    labels = preparation_labels(config, bool(copies))
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
    if env or command_env:
        config["has_env"] = True
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
                          str(int(color)), json.dumps(config | {"debug": debug, "progress_labels": labels}), *command])
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
            terminal_connect = bool(advertised) and any(key.split()[:2] == advertised for key in keys)
            private = public.with_suffix("")
            if not terminal_connect and private.is_file():
                try:
                    terminal_connect = not sp.run(
                        ["ssh-keygen", "-y", "-P", "", "-f", str(private)], stdin=sp.DEVNULL,
                        stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=2,
                    ).returncode
                except (OSError, sp.TimeoutExpired):
                    pass
        if terminal_connect:
            connect_options.extend(("-o", "BatchMode=yes"))
        pending = [f"{directory}/{file}" for directory, file in
                   ((drive_stage, "rclone.conf"), (jobdir, "env.json")) if file in staging.get(directory, {})]
        master_closed = False

        def close_master():
            nonlocal master_closed
            if master_closed:
                return
            master_closed = True
            try:
                if pending:
                    # The runner claims these names by rename; never remove active files.
                    command = shlex.join(["rm", "-f", "--", *pending])
                    try:
                        cleanup = sp.run(
                            [*ssh, "-T", "-o", "ProxyCommand=false", "-o", "ControlMaster=no", target, command],
                            stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=5,
                        )
                        confirmed = cleanup.returncode == 0
                    except (OSError, sp.TimeoutExpired):
                        confirmed = False
                    if not confirmed:
                        print(f"oncolab: could not confirm cleanup of unclaimed credentials for {name!r}",
                              file=sys.stderr)
            finally:
                sp.run([*ssh, "-O", "exit", target], stdout=sp.DEVNULL, stderr=sp.DEVNULL)

        stage = shlex.join(["python3", "-c", STAGE]) if staging else "true"
        try:
            with without_echo(terminal_connect):
                connected = run_step(CONNECTING, lambda: sp.run(
                    [*ssh, *connect_options, target, stage],
                    input=staged_input, capture_output=True,
                ), terminal_connect, color, labels=labels)
            if connected.returncode:
                show_ssh_error(connected.stderr, debug)
                return connected.returncode, True
            tty = "-t" if sys.stdin.isatty() and sys.stdout.isatty() else "-T"
            attempted.set()
            transfer = transfers.submit(upload, ssh, target, result, sources, targets, debug=debug) if sources else None
            task_upload = transfers.submit(upload_tasks, ssh, target, jobdir, groups, endpoint, debug=debug) if groups else None
            argv = [*ssh, "-o", "ProxyCommand=false", "-o", "ControlMaster=no", tty, target, payload]
            status = call_with_upload(argv, transfer) if transfer else sp.call(argv)
            if status == 0:
                pending.clear()
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
                if transfer_errors:
                    status = status or 1
            return status, False
        finally:
            close_master()


def show_ssh_error(stderr, debug):
    text = (stderr or b"").decode(errors="replace")
    # Never replay free-form ProxyCommand output. OpenSSH diagnostics and
    # the staging protocol can be reported without exposing URLs or tokens.
    if stage := re.search(r"^oncolab-stage: ([A-Za-z]+)$", text, re.MULTILINE):
        print("oncolab: Could not stage setup files on the Colab VM.", file=sys.stderr)
        if debug:
            print(f"oncolab: staging exception: {stage[1]}", file=sys.stderr)
        return
    reasons = (
        ("Address already in use", "A local port is already in use; choose a different port with -p."),
        ("Could not request local forwarding", "SSH could not set up local port forwarding."),
        ("Permission denied", "SSH permission denied; check your SSH key."),
        ("Host key verification failed", "SSH host key verification failed; check the trusted host key."),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED", "SSH host key changed; verify it before reconnecting."),
        ("Connection timed out", "SSH connection timed out."),
        ("Connection refused", "SSH connection was refused."),
        ("Connection closed", "The SSH connection closed before setup started."),
    )
    hint = next((message for marker, message in reasons if marker in text), None)
    print("oncolab: " + (hint or "SSH connection failed. Check your SSH key and Colab session."),
          file=sys.stderr)
    if debug:
        # Standard Python frame locations are useful without dumping source,
        # rich traceback locals, or a remote request's headers.
        for filename, number, function in re.findall(
                r'^\s*File "[^"\n]*/([A-Za-z0-9_.-]+)", line (\d+), in ([A-Za-z_<>]+)\s*$', text, re.MULTILINE):
            print(f"  {filename}:{number} in {function}", file=sys.stderr)


def debug_internal(exc):
    from traceback import extract_tb
    print(type(exc).__name__, file=sys.stderr)
    for frame in extract_tb(exc.__traceback__):
        print(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}", file=sys.stderr)


def main():
    try:
        return command_line()
    except KeyboardInterrupt:
        cancel_footer(False, not os.environ.get("NO_COLOR"))
        return 130
    except Exception as exc:
        # Includes failures that occur before an allocation can exist.
        print("oncolab: Unexpected internal error. Use --debug for details.", file=sys.stderr)
        if "--debug" in sys.argv[1:]:
            debug_internal(exc)
        return 1


def command_line():
    parser = argparse.ArgumentParser(description="Run a terminal command on a fresh Google Colab VM.")
    parser.add_argument("--version", action="version", version=f"oncolab {version('oncolab')}")
    parser.add_argument("--copy", action="append", default=[], metavar="LOCAL[:REMOTE]",
                        help="Copy a file or directory to the VM before running; repeatable")
    parser.add_argument("--copy-to", action="append", nargs=2, default=[], metavar=("LOCAL", "REMOTE"),
                        help="Copy using separate literal paths; repeatable")
    parser.add_argument("--mount", action="append", default=[], metavar="source=REMOTE:PATH,target=PATH[,readonly]",
                        help="Mount an rclone remote path; read/write by default, add ,readonly for read-only; repeatable")
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
    parser.add_argument("--attempts", type=int, default=4, metavar="N",
                        help="Total VM allocation attempts for temporary failures (default 4; 0 until cancelled)")
    parser.add_argument("--debug", action="store_true", help="Show internal diagnostic details")
    parser.add_argument("--high-mem", action="store_true", help="Request a high-memory VM")
    parser.add_argument("-A", "--forward-agent", action="store_true", help="Forward the local SSH agent")
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="COMMAND")
    args = parser.parse_args()
    if args.attempts < 0:
        parser.error("--attempts must be nonnegative")
    for option, models in (("gpu", ("T4", "L4", "G4", "A100", "H100")), ("tpu", ("V5E1", "V6E1"))):
        if value := getattr(args, option):
            if value.upper() not in models:
                parser.error(f"--{option} must be one of {', '.join(models)}")
            setattr(args, option, value.upper())
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    try:
        config, rclone_path, known_hosts = plan(args.config, cli_mounts=args.mount, cli_ports=args.publish)
        config["command_env"] = parse_env(args.env)
        copies, task_groups = prepare_copies(config, args.copy, args.copy_to)
        ini = read_rclone_config(rclone_path) if rclone_path is not None else None
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    color = not os.environ.get("NO_COLOR")
    terminal = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    with TemporaryDirectory(prefix="oncolab-session-") as directory:
        return execute(args, command, config, ini, known_hosts, copies, task_groups,
                       terminal, color, Path(directory) / "sessions.json")


def execute(args, command, config, ini, known_hosts, copies, task_groups, terminal, color, store):
    name = None
    attempted = Event()
    try:
        name, status = allocate(args, terminal, color, store, preparation_labels(config, bool(copies)))
        if not name:
            return status
        status, fallback = run_session(name, config, ini, known_hosts, command,
                                       args.forward_agent, terminal, color, attempted, copies=copies,
                                       store=store, debug=args.debug, task_copies=task_groups)
        if fallback:
            release(name, terminal, color, store)
        if attempted.is_set() and (status == 255 or status < 0):
            print(f"oncolab: SSH status is ambiguous; {allocation_hint(name, store)}", file=sys.stderr)
        return status if status >= 0 else 128 - status
    except KeyboardInterrupt:
        # After SSH takes ownership, the existing remote teardown owns release.
        if name is None and store.is_file():
            # SIGINT may land between the allocator restoring the signal
            # handler and the caller storing its successful return value.
            saved = json.loads(store.read_text())
            if len(saved) == 1:
                name = next(iter(saved))
        if name and not attempted.is_set():
            previous = signal(SIGINT, SIG_IGN)
            try:
                release(name, terminal, color, store)
            finally:
                signal(SIGINT, previous)
        elif name:
            print(f"oncolab: {allocation_hint(name, store)} if it is still running", file=sys.stderr)
        cancel_footer(False, color)
        return 130
    except (AllocationError, OSError) as exc:
        print(f"oncolab: {exc}", file=sys.stderr)
    except Exception as exc:
        print("oncolab: Unexpected internal error. Use --debug for details.", file=sys.stderr)
        if args.debug:
            debug_internal(exc)
    if name and not attempted.is_set():
        release(name, terminal, color, store)
    elif name:
        print(f"oncolab: {allocation_hint(name, store)} if it is still running", file=sys.stderr)
    return 1
