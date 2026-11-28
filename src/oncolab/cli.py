import argparse
import asyncio
import base64
import inspect
import json
import math
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess as sp
import sys
import termios
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, suppress
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from signal import SIGINT, SIG_IGN, signal
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
from time import monotonic, sleep

from . import diagnostics, ssh_pipeline
from .allocation import AllocationError, AllocationLabel, Cancellation, run_allocator, write_diagnostic
from .config import plan, read_rclone_config
from .copy import ARCHIVE_WORKERS, COPY_FAILED, INPUT_CLOSED, copy_plan, send_archive, send_input
from .diagnostics import outcome, safe_subprocess_details
from .duration import format_duration
from .env import parse_env
from .progress import (
    ALLOCATING, CONNECTING, STARTING_RUNNER, WAITING, SetupTimeline, completed_steps, display_label, preparation_labels,
    progress, run_step, setup_summary_socket,
)


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


def stored_session_name(store):
    try:
        state = json.loads(store.read_text())
        if isinstance(state, dict) and len(state) == 1:
            name = next(iter(state))
            if re.fullmatch(r"oncolab-[0-9a-f]{12}", name):
                return name
    except (OSError, ValueError):
        pass
    return None


def allocation_hint(name, store):
    try:
        endpoint = json.loads(store.read_text())[name]["endpoint"] if store else None
    except (OSError, ValueError, KeyError, TypeError):
        endpoint = None
    if isinstance(endpoint, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", endpoint):
        return f"Check 'colab sessions' for VM {endpoint!r}"
    return "Check 'colab sessions' for the VM"


def release(name, terminal, color, store=None, *, debug=False):
    hint = allocation_hint(name, store)
    details = []
    with without_echo(terminal):
        try:
            result = run_step("Releasing Colab VM…", lambda: sp.run(
                [*colab_command(store), "stop", "-s", name], capture_output=True,
                stdin=sp.DEVNULL, timeout=12,
            ), terminal, color, timed=False)
            if result.returncode == 0 and b"Session terminated." in result.stdout:
                return
            if debug:
                details.append(f"  colab stop exited with status {result.returncode}")
                text = (result.stderr or b"").decode(errors="replace")
                details.extend(safe_subprocess_details(text))
        except sp.TimeoutExpired:
            details.append("  colab stop timed out")
        except OSError as exc:
            details.append(f"  {type(exc).__name__} starting colab stop")
    write_diagnostic(outcome("VM release unconfirmed", hint, color=color))
    if debug:
        for detail in details:
            write_diagnostic(detail)


def allocate(args, terminal, color, store, labels):
    cancellation = Cancellation()
    name = "oncolab-" + secrets.token_hex(6)
    try:
        previous = signal(SIGINT, cancellation.request)
        try:
            started = monotonic()
            with without_echo(terminal) as echo_was_on:
                status = run_allocator(args, store, name, terminal, color, cancellation, labels)
        finally:
            # Stop recording cancellation before choosing the outcome.
            # A subsequent Ctrl+C reaches execute's pre-SSH cleanup.
            signal(SIGINT, previous)
        if (cancellation.requested or status != 0) and store.is_file():
            cleanup_handler = signal(SIGINT, SIG_IGN)
            try:
                release(name, terminal, color, store, debug=args.debug)
            finally:
                signal(SIGINT, cleanup_handler)
        if cancellation.requested or status == 130:
            cancel_footer(echo_was_on, color)
            return None, 130
        if status == 0 and getattr(args, "verbose", False):
            completed_steps([(0, ALLOCATING, started, monotonic())], labels, color)
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
        write_diagnostic(prefix + "[cancelled]")


def ssh_options(name, store=None):
    proxy = [*colab_command(store), "ssh", "--proxy-mode"]
    if store is None:
        proxy += ["-s", name]
    # With a private one-entry store, let the proxy resolve its sole entry.
    # A missing explicit name would make colab ssh auto-create another VM.
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
    staging = json.load(sys.stdin)
    if isinstance(staging, list):
        clear, staging = staging
        for file in clear:
            Path(file).unlink(missing_ok=True)
    for directory, entries in staging.items():
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

def show_remote_handoff(color):
    # Give startup diagnostics their own row even if SSH dies before
    # Python takes over. The remote runner reuses this row on success.
    columns = os.get_terminal_size(sys.stdout.fileno()).columns or 80
    encoding = sys.stdout.encoding or "utf-8"
    marker, ellipsis = "⠛", "…"
    try:
        (marker + ellipsis).encode(encoding)
    except UnicodeEncodeError:
        marker, ellipsis = "o", "..."
    label = f"{marker} Waiting for remote setup to start{ellipsis}"
    width = max(1, columns - 1)
    if len(label) > width:
        label = label[:max(0, width - len(ellipsis))] + ellipsis[:width]
    if color:
        # Match the spinner (ansigray) and label (dim) of live progress.
        label = "\x1b[37m" + label[:1] + "\x1b[0;2m" + label[1:] + "\x1b[0m"
    sys.stdout.buffer.write((label + "\n").encode(encoding))
    sys.stdout.buffer.flush()


def erase_remote_handoff():
    sys.stdout.write("\x1b[A\r\x1b[K")
    sys.stdout.flush()


def show_owner_errors(stream, debug):
    stream.seek(0)
    shown = False
    for line in stream.read(16_384).decode(errors="replace").splitlines():
        if line.removeprefix("\x1b[31m").startswith(
            ("[Remote preparation failed]", "[oncolab failed]", "[Check VM state]",
             "[VM left running]", "[VM release unconfirmed]"),
        ):
            write_diagnostic(line)
            shown = True
        elif debug and shown and re.fullmatch(r"(?:[A-Za-z_]+(?:Error|Exception)|"
                                              r"  [A-Za-z0-9_.<>-]+:\d+ in [A-Za-z0-9_<>.]+)", line):
            write_diagnostic(line)
    return shown


def session_open_refused(log):
    # OpenSSH reports this MUX_S_FAILURE before sending the remote command.
    # A broken pipe or missing reply does not establish that boundary.
    return (b"master session id:" not in log and b"Received exit status from master" not in log
            and any(re.search(rb"mux_client_request_session(?:\(\))?:.*"
                              rb"session request failed: Session open refused by peer\s*$", line)
                    for line in log.splitlines()))


def call_command_ssh(argv, log_path, transfer, owner, *, on_wait_cancel=None):
    for pause in (1, 2, None):
        log_path.unlink(missing_ok=True)
        status = call_with_upload(argv, transfer) if transfer else sp.call(argv)
        log = log_tail(log_path) if status == 255 else b""
        refused = session_open_refused(log)
        if not refused or owner is None or pause is None or owner.poll() is not None:
            return status, log, refused
        try:
            sleep(pause)
        except KeyboardInterrupt:
            if on_wait_cancel:
                on_wait_cancel()
            raise
        if owner.poll() is not None:
            return status, log, refused


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
                    write_diagnostic(outcome("Copy failed", error))
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
            write_diagnostic(outcome("Copy failed", error))
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


async def bootstrap_ssh(argv, data):
    # Stay in the caller's foreground process group so OpenSSH can still read
    # a password or key passphrase from /dev/tty. Own cancellation explicitly:
    # a signal may reach oncolab alone rather than the whole process group.
    loop = asyncio.get_running_loop()
    spawn = asyncio.create_task(loop.subprocess_exec(
        lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=64 * 1024, loop=loop),
        *argv, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
    ))
    transport = exchange = None
    try:
        transport, protocol = await asyncio.shield(spawn)
        proc = asyncio.subprocess.Process(transport, protocol, loop)
        exchange = asyncio.create_task(proc.communicate(data))
        stdout, stderr = await asyncio.shield(exchange)
        return sp.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except asyncio.CancelledError:
        # Shield the short spawn so even Ctrl+C during startup cannot leave
        # an unowned SSH process. OpenSSH's exit handler closes its proxy.
        transport = transport or (await spawn)[0]
        with suppress(ProcessLookupError):
            transport.terminate()
        try:
            if exchange is not None:
                await asyncio.wait_for(asyncio.shield(exchange), timeout=1)
        except asyncio.TimeoutError:
            # SSH helpers may still have inherited stderr open even when
            # SSH itself exits. Do not wait on their pipe indefinitely.
            transport.close()
            await asyncio.gather(exchange, return_exceptions=True)
        finally:
            transport.close()
        raise


def log_tail(path):
    try:
        with path.open("rb") as log:
            log.seek(max(0, path.stat().st_size - 16_384))
            return log.read(16_384)
    except OSError:
        return b""


def feed_staging(stream, data):
    with stream:
        try:
            stream.write(data)
        except BrokenPipeError:
            pass


def needs_summary_relay(tty):
    # A PTY combines remote stdout and stderr even when both local fds name
    # the same device. For pipes/sockets, keep remote SSH free of diagnostics
    # it could be unable to drain into the actual local stderr.
    if tty == "-t":
        return True
    try:
        mode = os.fstat(sys.stderr.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
    except (OSError, ValueError):
        return False


def read_setup_summary(path, stop, labels, color):
    while not stop.is_set():
        try:
            with socket.socket(socket.AF_UNIX) as peer:
                peer.settimeout(.25)
                peer.connect(path)
                buffer = bytearray()
                while not stop.is_set():
                    try:
                        data = peer.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        break  # The remote listener may not be up yet.
                    buffer.extend(data)
                    if len(buffer) > ssh_pipeline.CONTROL_LIMIT:
                        return
                    if buffer.endswith(b"\n"):
                        rows = json.loads(buffer)
                        if not isinstance(rows, list) or any(
                            not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int or row[0] not in (0, 2)
                            or not isinstance(row[1], str)
                            or any(type(n) not in (int, float) or not math.isfinite(n) for n in row[2:])
                            or row[2] > row[3] for row in rows
                        ):
                            return
                        try:
                            completed_steps(rows, labels, color)
                        finally:
                            peer.sendall(b"1")
                        return
        except (OSError, OverflowError, ValueError):
            pass
        stop.wait(.12)


async def wait_setup_listener(process):
    loop = asyncio.get_running_loop()
    fd = process.stdout.fileno()
    pending = loop.create_future()
    partial = b""

    def read():
        nonlocal partial
        if pending.done():
            return
        try:
            data = os.read(fd, 65536)
            if not data:
                pending.set_result(False)
            else:
                partial += data
                if ssh_pipeline.SETUP_LISTENING in partial.splitlines(keepends=True):
                    pending.set_result(True)
                elif len(partial) > 65536:
                    pending.set_result(False)
        except OSError as exc:
            pending.set_exception(exc)

    loop.add_reader(fd, read)
    try:
        return await pending
    finally:
        loop.remove_reader(fd)


def embedded_gate(protocol, render_source):
    return "\n".join((*render_source, protocol, "raise SystemExit(command_gate_main(progress))"))


def run_session(name, config, rclone_ini, known_hosts, command, forward, terminal, color, attempted, *,
                copies=(), task_copies=None, store=None, debug=False, connect_label=CONNECTING, unclaimed=None, verbose=False):
    proxy, identity, public = ssh_options(name, store)
    target = "root@colab-runtime"
    drive_stage = "/dev/shm/oncolab-" + secrets.token_hex(12)
    render_source = tuple(inspect.getsource(item) for item in
                          (format_duration, display_label, SetupTimeline, completed_steps, setup_summary_socket, progress))
    remote = "\n".join((f"_ONCOLAB_DEBUG = {debug!r}", *render_source, inspect.getsource(diagnostics),
                        files("oncolab").joinpath("remote.py").read_text()))
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
    tty = "-t" if sys.stdin.isatty() and sys.stdout.isatty() else "-T"
    live_payload = terminal and tty == "-t"
    verbose = verbose and sys.stderr is not None
    summary_path = drive_stage + "-summary.sock" if verbose and needs_summary_relay(tty) else None
    if summary_path:
        config["summary_path"] = summary_path
    config.update(debug=debug, verbose=verbose, progress_labels=labels, recovery_hint=allocation_hint(name, store))
    if live_payload:
        config["terminal_handoff"] = STARTING_RUNNER
    parallel = os.environ.get("ONCOLAB_PARALLEL_SETUP", "1") == "1"
    python = ["uv", "run", "--no-project", "python3", "-c"]
    owner_code = None
    if parallel:
        control = drive_stage + "-gate/control.sock"
        protocol = files("oncolab").joinpath("ssh_pipeline.py").read_text()
        remote = protocol + "\n" + remote
        gate_code = embedded_gate(protocol, render_source)
        command_plan = {key: config[key] for key in ("terminal_handoff", "progress_labels", "recovery_hint", "verbose", "summary_path") if key in config}
        command_plan.update(pipeline_role="command")
        payload = shlex.join([*python, gate_code, control, str(int(color)), json.dumps(command_plan),
                              *config["entrypoint"], *command])
        config.pop("terminal_handoff", None)
        config.update(pipeline_role="setup", pipeline_control=control, pipeline_terminal=live_payload,
                      pipeline_dirs={path: {name: value for name, value in entries.items()
                                           if name not in ("env.json", "rclone.conf")}
                                     for path, entries in staging.items()})
        owner_code = shlex.join([*python, remote, drive_stage, str(int(color)), json.dumps(config)])
        # The setup channel is detached; mirror the TERM that OpenSSH sends
        # on the command's PTY without overriding a profile value.
        setup_env = {"TERM": os.environ.get("TERM", "dumb"), **env} if tty == "-t" else env
        staged_input = ssh_pipeline.packet(setup_env, command_env)
        if rclone_ini is not None:
            data = rclone_ini.encode() if isinstance(rclone_ini, str) else rclone_ini
            staged_input += (json.dumps(base64.b64encode(data).decode()) + "\n").encode()
    else:
        encoded_staging = {
            directory: {name: base64.b64encode(value.encode() if isinstance(value, str) else value).decode("ascii")
                        for name, value in entries.items()}
            for directory, entries in staging.items()
        }
        if unclaimed:
            # A previous attempt can lose its acknowledgement after writing
            # credentials. Scrub only the old unclaimed paths.
            staged_input = json.dumps([sorted(unclaimed), encoded_staging]).encode()
        else:
            staged_input = json.dumps(encoded_staging).encode() if staging else b""
        payload = shlex.join([*python, remote, drive_stage, str(int(color)), json.dumps(config), *command])
    trust = (["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
             if known_hosts else ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}"])
    with (TemporaryDirectory(prefix="oc-", dir="/tmp") as tmp,
          ThreadPoolExecutor(3 if parallel else 2) as transfers,
          (TemporaryFile() if parallel else nullcontext()) as owner_errors):
        ssh = ["ssh", "-F", os.devnull, "-S", f"{tmp}/ssh", "-o", "LogLevel=ERROR", "-A" if forward else "-a"]
        # ControlPersist can hide ProxyCommand stderr before SSH exits,
        # including Colab's definitive authentication diagnostics.
        proxy_log = Path(tmp) / "proxy-ssh.log"
        proxy_log.touch(mode=0o600)
        proxy_command = f"{shlex.join(proxy)} 2> {shlex.quote(str(proxy_log))}"
        connect_options = [*identity, "-M", "-T", "-o", "ControlPersist=60",
                           "-o", "ConnectTimeout=30", "-o", "ConnectionAttempts=1",
                           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                           "-o", "ProxyCommand=" + proxy_command.replace("%", "%%"), *trust]
        endpoint = f"{tmp}/stdin.sock" if any(source is not None for _, source in groups.values()) else None
        if ports or endpoint:
            connect_options.extend(("-o", "ExitOnForwardFailure=yes"))
        if endpoint:
            connect_options.extend(("-L", f"{endpoint}:{jobdir}/stdin.sock"))
        summary_stop = Event()
        summary_reader = None
        local_summary = f"{tmp}/summary.sock" if summary_path else None
        if local_summary:
            connect_options.extend(("-L", f"{local_summary}:{summary_path}"))
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
        if parallel:
            pending = []  # The setup owner directly claims its staged files.
        master_closed = False
        bootstrap_ready = False
        owner = None

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
                        if not bootstrap_ready and unclaimed is not None:
                            unclaimed.update(pending)
                        else:
                            unclaimed_credentials(name, color)
            finally:
                try:
                    sp.run([*ssh, "-O", "exit", target], stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=5)
                except (OSError, sp.TimeoutExpired):
                    pass

        stage = shlex.join(["python3", "-c", STAGE]) if staged_input else "true"

        async def connect():
            # Authenticate without dispatching a runner in parallel mode.
            # A failed connection is then safe to retry on the same VM.
            if parallel:
                return await bootstrap_ssh([*ssh, *connect_options, "-N", "-f", target], b"")
            return await bootstrap_ssh([*ssh, *connect_options, target, stage], staged_input)

        try:
            connect_started = monotonic()
            with without_echo(terminal_connect):
                connected = run_step(connect_label, connect, terminal_connect, color, labels=labels)
            if connected.returncode:
                if connected.returncode in (-SIGINT, 130):
                    raise KeyboardInterrupt
                diagnostic = (connected.stderr or b"") + b"\n" + log_tail(proxy_log)
                failure = SSHBootstrapError(connected.returncode, diagnostic)
                if failure.rejected:
                    pending.clear()
                raise failure
            connect_ended = monotonic()
            if verbose and not parallel:
                completed_steps([(0, CONNECTING, connect_started, connect_ended)], labels, color)
            with suppress(OSError):
                proxy_log.unlink(missing_ok=True)
            bootstrap_ready = True
            if unclaimed is not None and not parallel:
                unclaimed.clear()
            if live_payload:
                show_remote_handoff(color)
            if parallel:
                # Keep the status visible while the first exec publishes its
                # socket. Setup and config upload then overlap the second exec.
                owner_log = Path(tmp) / "owner-ssh.log"
                attempted.set()
                try:
                    owner = sp.Popen(
                        [*ssh, "-E", str(owner_log), "-T", "-o", "ProxyCommand=false",
                         "-o", "ControlMaster=no", target, owner_code],
                        stdin=sp.PIPE, stdout=sp.PIPE, stderr=owner_errors,
                    )
                except OSError:
                    attempted.clear()
                    if live_payload:
                        erase_remote_handoff()
                    raise
                transfers.submit(feed_staging, owner.stdin, staged_input)

                async def wait_owner():
                    return await asyncio.wait_for(wait_setup_listener(owner), timeout=45)

                try:
                    ready = asyncio.run(wait_owner())
                except TimeoutError:
                    ready = False
                except KeyboardInterrupt:
                    if live_payload:
                        erase_remote_handoff()
                    raise
                if not ready:
                    if live_payload:
                        erase_remote_handoff()
                    log = log_tail(owner_log)
                    try:
                        status = owner.wait(timeout=1) or 255
                    except sp.TimeoutExpired:
                        status = 255
                    if session_open_refused(log):
                        attempted.clear()
                        raise SSHBootstrapError(status, log)
                    if not show_owner_errors(owner_errors, debug):
                        owner_errors.seek(0)
                        startup = owner_errors.read(16_384).decode(errors="replace")
                        if status == 127 and re.search(r"(?m)^(?:[^\n]*: )?uv: (?:command )?not found\s*$",
                                                       startup):
                            attempted.clear()
                            write_diagnostic(outcome("Setup could not start",
                                                     "uv is not available on the Colab VM.", color=color))
                            return 127, True
                        show_ssh_error(log, debug, recovery=allocation_hint(name, store), status=status)
                    return status, False
                if verbose:
                    if live_payload:
                        erase_remote_handoff()
                    completed_steps([(0, CONNECTING, connect_started, connect_ended)], labels, color)
                    if live_payload:
                        show_remote_handoff(color)
            if local_summary:
                summary_reader = Thread(target=read_setup_summary,
                                        args=(local_summary, summary_stop, labels, color), daemon=True)
                summary_reader.start()
            # No replay after dispatch becomes possible: a lost mux response
            # does not establish whether the remote runner launched.
            attempted.set()
            transfer = transfers.submit(upload, ssh, target, result, sources, targets, debug=debug) if sources else None
            task_upload = transfers.submit(upload_tasks, ssh, target, jobdir, groups, endpoint, debug=debug) if groups else None
            # Keep native SSH errors separate from the command's stderr.
            # Mux and payload failures can both return 255; only a captured
            # SSH error is evidence for reporting a connection failure.
            ssh_log = Path(tmp) / "payload-ssh.log"
            # Force only mux-status logging. Globally raising LogLevel
            # would make OpenSSH print its TTY close footer to the user.
            mux_status_logging = ["-o", "IgnoreUnknown=LogVerbose",
                                  "-o", "LogVerbose=*:mux_client_request_session():*"]
            argv = [*ssh, *mux_status_logging, "-E", str(ssh_log), "-o", "ProxyCommand=false",
                    "-o", "ControlMaster=no", tty, target, payload]
            status, ssh_error, refused = call_command_ssh(
                argv, ssh_log, transfer, owner, on_wait_cancel=erase_remote_handoff if live_payload else None,
            )
            if live_payload and refused:
                erase_remote_handoff()
            if status == 255:
                if error := ssh_transport_error(ssh_error):
                    show_ssh_error(error, debug, recovery=allocation_hint(name, store))
            if owner is not None:
                try:
                    # A rejected payload can't join the gate. Let the owner
                    # time out and clean up before closing its transport.
                    owner_status = owner.wait(timeout=25 if refused else 2)
                except sp.TimeoutExpired:
                    owner_status = None
                if owner_status != status:
                    write_diagnostic(outcome("SSH setup outcome unconfirmed",
                                             allocation_hint(name, store) + ".", color=color))
                    status = status or 125
            if owner is not None and status:
                # The runner can fail before the display channel connects.
                # Never replay raw first-channel ProxyCommand diagnostics.
                show_owner_errors(owner_errors, debug)
            if status == 0:
                pending.clear()
            if status and ((transfer and not transfer.done()) or (task_upload and not task_upload.done())):
                close_master()
            if transfer:
                error, _ = transfer.result()
                if error:
                    write_diagnostic(outcome("Copy failed", error, color=color))
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
            summary_stop.set()
            try:
                close_master()
            finally:
                try:
                    if owner is not None:
                        try:
                            owner.wait(timeout=5)
                        except sp.TimeoutExpired:
                            owner.kill()
                            owner.wait()
                        owner.stdout.close()
                finally:
                    if summary_reader is not None:
                        summary_reader.join(.3)


def ssh_transport_error(log):
    # OpenSSH uses 255 for a missing exit packet as well as a valid 255
    # in an exit packet. Ignore progress/debug chatter on valid exits.
    if b"Received exit status from master" in log:
        return b""
    lost = b"Control master terminated unexpectedly"
    if lost in log:
        return lost
    errors = (b"mux_client_request_session:", b"mux_client_request_session():",
              b"mux_client_read_packet:", b"Connection closed",
              b"Connection timed out", b"Connection refused", b"Permission denied", b"proxy dialer failed")
    return b"\n".join(line for line in log.splitlines()
                      if not line.startswith(b"debug") and any(marker in line for marker in errors))


def unclaimed_credentials(name, color):
    write_diagnostic(outcome("VM cleanup unconfirmed",
                             f"Could not confirm cleanup of unclaimed credentials for {name!r}.",
                             color=color))


def ssh_failure_reason(text):
    permanent = (
        ("no SSH public key found", "No SSH public key was found; create one with ssh-keygen -t ed25519."),
        ("[colab] failed to derive public key", "Colab could not read the SSH public key; check your SSH key."),
        ("[colab] ssh-keygen produced no key for ", "Colab could not read the SSH public key; check your SSH key."),
        ("[colab] Server rejected request: missing pubkey header", "Colab did not receive an SSH public key."),
        ("[colab] Server rejected pubkey: unsupported key type", "Colab rejected your SSH key type; use Ed25519 or ECDSA."),
        ("[colab] Server rejected pubkey:", "Colab rejected your public key; check your SSH key."),
        ("[colab] Server rejected pubkey (HTTP 400)", "Colab rejected your public key; check your SSH key."),
        ("[colab] Authentication failed (HTTP 401)", "Colab rejected the connection credentials for this VM."),
        ("To authorize colab-cli, visit this URL in any browser:", "Sign in to Colab with 'colab sessions' and try again."),
        ("No valid default credentials found.", "Sign in to Colab with 'colab sessions' and try again."),
        ("[colab] Forbidden (HTTP 403)", "Colab did not authorize SSH access to this VM."),
        ("[colab] Endpoint not found (HTTP 404)", "This Colab runtime does not expose an SSH endpoint."),
        ("[colab] Error: No active sessions found.", "Colab no longer has an active session for this VM."),
        ("Host key verification failed", "SSH host key verification failed; check the trusted host key."),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED", "SSH host key changed; verify it before reconnecting."),
        ("Address already in use", "A local port is already in use; choose a different port with -p."),
        ("Could not request local forwarding", "SSH could not set up local port forwarding."),
        ("Bad configuration option:", "OpenSSH rejected a local connection option."),
        ("Too many authentication failures", "SSH permission denied; check your SSH key."),
    )
    for marker, message in permanent:
        if marker in text:
            return message, True
    if re.search(r"(?m)^\[colab\] --identity [^\n]+: file not found\.", text):
        return "Colab could not find the configured SSH key.", True
    if re.search(r"(?mi)^Unable to negotiate with [^\n]*: no matching (?:host key type|key exchange method|cipher|MAC) found", text):
        return "OpenSSH and the VM could not agree on an SSH algorithm.", True
    if re.search(r"(?mi)^\[colab\] session '[^'\n]+' not found\.", text):
        return "Colab no longer has an active session for this VM.", True
    # A ProxyCommand may itself report a generic permission error; only
    # recognize a positive OpenSSH authentication rejection here.
    if re.search(r"(?m)^(?:[^\s]+: )?Permission denied(?:[.!]?$| \(|, please try again)", text):
        return "SSH permission denied; check your SSH key.", True
    transient = (
        ("Already-active SSH session", "Colab already has an active SSH connection to this VM."),
        ("SSH session limit reached", "This VM has reached the Colab SSH session limit."),
        ("Bad gateway (HTTP 502)", "Colab could not reach the VM's SSH server."),
        ("Connection timed out", "SSH connection timed out."),
        ("Connection refused", "SSH connection was refused."),
        ("Connection reset", "The SSH connection was reset."),
        ("Connection closed", "The SSH connection closed."),
        ("Session open refused by peer", "The VM refused to open the SSH command channel."),
        ("mux_client_request_session:", "The SSH session could not be opened."),
        ("mux_client_request_session():", "The SSH session could not be opened."),
        ("mux_client_read_packet:", "The SSH connection closed."),
        ("Control master terminated unexpectedly", "The SSH connection closed before reporting an exit status."),
    )
    return next((message for marker, message in transient if marker in text), None), False


def staging_error(text):
    return re.search(r"^oncolab-stage: ([A-Za-z]+)$", text, re.MULTILINE)


class SSHBootstrapError(Exception):
    def __init__(self, status, stderr):
        self.status = status
        self.stderr = stderr
        text = (stderr or b"").decode(errors="replace")
        self.reason, self.rejected = ssh_failure_reason(text)
        # Non-255 positive exits report a remote stage failure. A generic
        # 255 gives no such evidence; reconnect to the same allocated VM.
        self.retry = (status == 255 or status < 0) and not self.rejected and not staging_error(text)


def show_ssh_error(stderr, debug, *, recovery=None, status=255):
    text = (stderr or b"").decode(errors="replace")
    # Never replay free-form ProxyCommand output. OpenSSH diagnostics and
    # the staging protocol can be reported without exposing URLs or tokens.
    stage = staging_error(text)
    if stage or 0 < status < 255:
        message = ("Could not stage setup files on the Colab VM." if stage
                   else f"The SSH bootstrap command on the Colab VM exited with status {status}.")
        write_diagnostic(outcome("Setup could not start", message))
        if debug and stage:
            write_diagnostic(f"oncolab: staging exception: {stage[1]}")
        return
    hint, _ = ssh_failure_reason(text)
    message = hint or "OpenSSH could not complete the session."
    if recovery:
        message += " " + recovery + "."
    write_diagnostic(outcome("SSH connection failed", message))
    if debug:
        # Standard Python frame locations are useful without dumping source,
        # rich traceback locals, or a remote request's headers.
        for detail in safe_subprocess_details(text):
            write_diagnostic(detail)


def debug_internal(exc):
    from traceback import extract_tb
    write_diagnostic(type(exc).__name__)
    for frame in extract_tb(exc.__traceback__):
        write_diagnostic(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")


def main():
    args = None
    try:
        parser, args, command = parse_args()
        return command_line(parser, args, command)
    except KeyboardInterrupt:
        cancel_footer(False, not os.environ.get("NO_COLOR"))
        return 130
    except Exception as exc:
        # Includes failures that occur before an allocation can exist.
        write_diagnostic(outcome("oncolab failed", "Unexpected internal error. Use --debug for details."))
        if args is not None and args.debug:
            debug_internal(exc)
        return 1


def parse_args():
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
    parser.add_argument("--attempts", type=int, metavar="N",
                        help="Attempts per VM allocation and SSH connection (default 4; 0 until cancelled)")
    parser.add_argument("--verbose", action="store_true", help="Keep completed preparation timings on stderr")
    parser.add_argument("--debug", action="store_true", help="Show internal diagnostic details")
    parser.add_argument("--high-mem", action="store_true", help="Request a high-memory VM")
    parser.add_argument("-A", "--forward-agent", action="store_true", help="Forward the local SSH agent")
    parser.add_argument("command", nargs=argparse.REMAINDER, metavar="COMMAND")
    args = parser.parse_args()
    def retry_setting(name, parse, fallback, minimum, maximum):
        try:
            value = parse(os.environ.get(name) or fallback)
            if not minimum <= value <= maximum:
                raise ValueError
            return value
        except ValueError:
            parser.error(f"{name} must be between {minimum} and {maximum}")

    if args.attempts is None:
        args.attempts = 1 + retry_setting("COLAB_CLI_ASSIGN_RETRIES", lambda x: max(0, int(x)), "3", 0, 10_000)
    args.request_timeout = retry_setting("COLAB_CLI_REQUEST_TIMEOUT", float, "120", .1, 3600)
    args.retry_backoff = retry_setting("COLAB_CLI_RETRY_BACKOFF", lambda x: max(float(x), 0), "1", 0, 120)
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
    return parser, args, command


def command_line(parser, args, command):
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


def retry_ssh(name, args, config, ini, known_hosts, command, copies, task_groups, terminal, color, store, attempted):
    labels = preparation_labels(config, bool(copies))
    unclaimed = set()
    connection = AllocationLabel()
    wait = AllocationLabel()
    number = 0
    try:
        while True:
            number += 1
            count = f"{number}/{args.attempts}" if args.attempts else str(number)
            prefix = f"SSH · attempt {count}"
            connection.state = (CONNECTING, "", None, prefix)
            try:
                return run_session(name, config, ini, known_hosts, command,
                                   args.forward_agent, terminal, color, attempted, copies=copies,
                                   store=store, debug=args.debug, task_copies=task_groups,
                                   connect_label=connection, unclaimed=unclaimed, verbose=args.verbose)
            except SSHBootstrapError as exc:
                if attempted.is_set():
                    raise
                if not exc.retry or (args.attempts and number >= args.attempts):
                    show_ssh_error(exc.stderr, args.debug, status=exc.status)
                    return exc.status, True
                delay = min(120, args.retry_backoff * 2 ** min(number - 1, 16))
                if delay:
                    reason = exc.reason or "OpenSSH could not complete the session."
                    wait.state = (WAITING, reason, monotonic() + delay, prefix)

                    async def backoff():
                        await asyncio.sleep(delay)

                    with without_echo(terminal):
                        run_step(wait, backoff, terminal, color, labels=labels)
    finally:
        if unclaimed:
            unclaimed_credentials(name, color)


def execute(args, command, config, ini, known_hosts, copies, task_groups, terminal, color, store):
    name = None
    attempted = Event()
    try:
        name, status = allocate(args, terminal, color, store, preparation_labels(config, bool(copies)))
        if not name:
            return status
        status, fallback = retry_ssh(name, args, config, ini, known_hosts, command, copies,
                                     task_groups, terminal, color, store, attempted)
        if fallback:
            release(name, terminal, color, store, debug=args.debug)
        # OpenSSH's positive 255 is indistinguishable from a legitimate
        # remote exit 255. Do not diagnose the payload's exit status.
        if attempted.is_set() and status < 0:
            write_diagnostic(outcome("SSH outcome unconfirmed", allocation_hint(name, store), color=color))
        return status if status >= 0 else 128 - status
    except KeyboardInterrupt:
        # After SSH takes ownership, the existing remote teardown owns release.
        # SIGINT can land before the caller receives the allocation result.
        name = name or stored_session_name(store)
        if name and not attempted.is_set():
            previous = signal(SIGINT, SIG_IGN)
            try:
                release(name, terminal, color, store, debug=args.debug)
            finally:
                signal(SIGINT, previous)
        elif name:
            write_diagnostic(outcome("VM status unconfirmed", allocation_hint(name, store) + ".", color=color))
        cancel_footer(False, color)
        return 130
    except (AllocationError, OSError) as exc:
        write_diagnostic(outcome("Could not start session", str(exc), color=color))
    except Exception as exc:
        write_diagnostic(outcome("oncolab failed", "Unexpected internal error. Use --debug for details.", color=color))
        if args.debug:
            debug_internal(exc)
    name = name or stored_session_name(store)
    if name and not attempted.is_set():
        release(name, terminal, color, store, debug=args.debug)
    elif name:
        write_diagnostic(outcome("VM status unconfirmed", allocation_hint(name, store) + ".", color=color))
    return 1
