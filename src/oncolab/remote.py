import asyncio
import base64
import fcntl
import json
import os
import shutil
import stat
import subprocess as sp
import sys
import time
from functools import partial
from pathlib import Path
from signal import SIGHUP, SIGINT, SIGKILL, SIGTERM, signal
from tempfile import TemporaryFile

if __package__:
    from .diagnostics import outcome, safe_subprocess_details
    from .progress import progress
    from .ssh_pipeline import (SETUP_LISTENING, CommandStateUnknown, SetupGate,
                               stdin_packets, staging_directories)


_ONCOLAB_COLOR = False


def report(label, message):
    print(outcome(label, message, color=_ONCOLAB_COLOR), file=sys.stderr)


def unassign():
    sp.Popen([sys.executable, "-P", "-c", r'''
import os
from time import sleep
from google.colab import runtime
key = b"TBE_RUNTIME_ADDR"
if key not in os.environb:
    os.environb[key] = next(
        entry.partition(b"=")[2] for entry in open("/proc/1/environ", "rb").read().split(b"\0")
        if entry.startswith(key + b"=")
    )
sleep(1)
runtime.unassign()
'''], cwd="/", start_new_session=True, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL)

# On Ubuntu 24.04 x86-64, rclone only needs the mount helper, not an apt upgrade.
FUSE = r'''
command -v fusermount3 >/dev/null && exit 0
. /etc/os-release
if [ "$ID:$VERSION_ID:$(uname -m)" = "ubuntu:24.04:x86_64" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    if curl -fsSL --max-time 12 https://archive.ubuntu.com/ubuntu/pool/main/f/fuse3/fuse3_3.14.0-5build1_amd64.deb -o "$tmp/fuse.deb" &&
       printf '%s  %s\n' 1e5580eeb72e830f0b242254b2438669a94c9912abf1e2f188b2992f188cc995 "$tmp/fuse.deb" | sha256sum -c --status &&
       dpkg-deb -x "$tmp/fuse.deb" "$tmp/root"; then
        helper=$(find "$tmp/root" -type f -name fusermount3 -print -quit)
        if [ -n "$helper" ] && "$helper" --version >/dev/null; then
            install -D -m 755 "$helper" "$HOME/.local/bin/fusermount3"
            exit 0
        fi
    fi
fi
apt-get update -qq && apt-get install -y -qq fuse3
'''
RCLONE = r'''
rclone rc --help 2>/dev/null | grep -q -- --unix-socket && exit 0
case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) echo "Unsupported rclone architecture" >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
version=v1.76.0
case "$arch" in
    amd64) sha=b00f00b3266c3764413afe599d3ec316f6f43b3e0ea8a0bc1d186d2133e46df7 ;;
    arm64) sha=ee169016675e23a69d77240565133ff7dbc9b33d16fa8f7053d4af39f1eaeb78 ;;
esac
archive="rclone-$version-linux-$arch"
curl -fsSL "https://downloads.rclone.org/$version/$archive.zip" -o "$tmp/rclone.zip"
printf '%s  %s\n' "$sha" "$tmp/rclone.zip" | sha256sum -c --status
unzip -p "$tmp/rclone.zip" "$archive/rclone" > "$tmp/rclone"
install -D -m 755 "$tmp/rclone" "$HOME/.local/bin/rclone"
'''

def drive_environment(profile):
    # Backend settings are useful, but the control socket belongs to the runner.
    client_options = {"RCLONE_LOOPBACK", "RCLONE_URL", "RCLONE_USER", "RCLONE_PASS",
                      "RCLONE_UNIX_SOCKET", "RCLONE_JSON"}
    env = {key: value for key, value in profile.items()
           if not (key == "RCLONE_RC" or key.startswith("RCLONE_RC_") or key in client_options)}
    managed = f"{env.get('HOME', str(Path.home()))}/.local/bin"
    paths = [managed, *env.get("PATH", "").split(os.pathsep),
             *os.environ.get("PATH", os.defpath).split(os.pathsep)]
    env["PATH"] = os.pathsep.join(dict.fromkeys(filter(None, paths)))
    return env


class DriveMounts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.config = self.directory / "active.conf"
        self.socket = self.directory / "rc.sock"
        self.attached = []
        self.server = None
        self.environment = None
        self.uncertain = False

    async def rc(self, method, **parameters):
        command = ["rclone", "--config", "/dev/null", "rc", "--unix-socket", str(self.socket),
                   method, "--json", json.dumps(parameters)]
        try:
            result = await command_result(command, self.environment, timeout=45)
        except TimeoutError as exc:
            if method == "mount/mount":
                # A timed-out client is not proof that the daemon did not mount.
                self.uncertain = True
            raise RuntimeError(f"rclone {method} timed out") from exc
        if result.returncode:
            raise RuntimeError(f"rclone {method} failed (exit {result.returncode})")

    async def start(self):
        self.server = sp.Popen(
            ["rclone", "--config", str(self.config), "rcd", "--rc-addr", str(self.socket), "--rc-no-auth"],
            env=self.environment, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            start_new_session=True,
        )
        for _ in range(150):
            if self.socket.is_socket():
                return
            if self.server.poll() is not None:
                break
            await asyncio.sleep(.1)
        raise RuntimeError("rclone did not open its private control socket")

    async def attach(self, mount):
        target = Path(mount["target"])
        target.mkdir(parents=True, exist_ok=True)
        if target.resolve() != target or any(target.iterdir()):
            raise RuntimeError(f"Mount target must be an empty directory without symlinks: {target}")

        async def mount_and_record():
            try:
                await self.rc("mount/mount", fs=mount["source"], mountPoint=str(target),
                              vfsOpt={"ReadOnly": mount["read_only"]})
            except RuntimeError:
                if target.is_mount():
                    self.attached.append(target)
                raise
            self.attached.append(target)

        operation = asyncio.create_task(mount_and_record())
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Record a mount completed concurrently with cancellation.
            await asyncio.gather(operation, return_exceptions=True)
            raise

    def seal(self):
        try:
            shutil.rmtree(self.directory)
        except FileNotFoundError:
            pass

    def detach(self, target):
        target = Path(target)
        if target in self.attached:
            if sp.call(["umount", "--", target]):
                report("VM left running", f"Could not unmount {target}.")
                return False
            self.attached.remove(target)
        return True

    def close(self, preserve=(), *, keep_daemon=False):
        self.seal()
        results = [self.detach(path) for path in reversed(self.attached[:]) if path not in preserve]
        if self.uncertain:
            report("VM left running", "Drive mount state is uncertain; the mount daemon is still running.")
        if not all(results) or self.attached or keep_daemon or self.uncertain:
            return False
        if self.server is not None:
            if not stop_process(self.server):
                return False
        return True


async def wait_copy(result, *, start_timeout=30, broker=None, input_pipe=None):
    path = Path(result)
    pid_path = path.with_name("copy-pid")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + start_timeout
    admission_deadline = None

    def read(path):
        try:
            return path.read_text()
        except FileNotFoundError:
            return ""

    statuses = ("ok", "failed", "short") if input_pipe else ("ok", "failed")
    while True:
        if input_pipe and path.with_name("writer-ready").exists():
            input_pipe.writer_attached()
        status = read(path)
        if status in statuses:
            if input_pipe:
                input_pipe.release("keeper")
            return sp.CompletedProcess([], int(status == "failed"), None, None)

        pid_text = read(pid_path)
        try:
            broker_text = read(broker) if broker else ""
            broker_alive = broker_text.endswith("\n")
            if broker_alive:
                os.kill(int(broker_text), 0)
                heartbeat = Path(broker).with_name("broker-heartbeat")
                try:
                    if time.time() - heartbeat.stat().st_mtime > max(5, start_timeout):
                        raise ProcessLookupError
                except FileNotFoundError:
                    # Startup writes the heartbeat before publishing the PID.
                    if loop.time() >= deadline:
                        raise ProcessLookupError
            if pid_text.endswith("\n"):
                os.kill(int(pid_text), 0)
            elif broker_alive and path.with_name("admitted").exists():
                if admission_deadline is None:
                    admission_deadline = loop.time() + start_timeout
                elif loop.time() >= admission_deadline:
                    raise ProcessLookupError
            elif not broker_alive and loop.time() >= deadline:
                raise ProcessLookupError
        except ProcessLookupError:
            if read(path) in statuses:
                continue  # Receiver may have published just before exiting.
            if broker:
                try:
                    path.with_name("cancelled").touch()
                except FileNotFoundError:
                    pass
            return sp.CompletedProcess([], 1, None, b"oncolab: copy receiver exited or did not start\n")
        await asyncio.sleep(.10)


def quiesce_archive(result, deadline):
    lock = Path(result).with_name("publication-lock")
    with lock.open("a") as publication:
        while True:
            try:
                fcntl.flock(publication, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(.05, remaining))


def enter_workdir(plan):
    workdir = Path(plan.get("workdir", "/content"))
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)


def stop_process(proc):
    try:
        os.killpg(proc.pid, SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except sp.TimeoutExpired:
        try:
            os.killpg(proc.pid, SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except sp.TimeoutExpired:
            report("Process still running", f"Could not stop process {proc.pid}.")
            return False
    # A shell may exit before its process group. Do not leave descendants alive.
    try:
        os.killpg(proc.pid, SIGKILL)
    except ProcessLookupError:
        pass
    return True


async def command_result(argv, env, stdin=sp.DEVNULL, *, timeout=None, on_start=None):
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=stdin, stdout=sp.PIPE, stderr=sp.PIPE, start_new_session=True,
    )
    try:
        if on_start:
            on_start()
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.CancelledError, OSError):  # Includes the builtin TimeoutError from wait_for.
        for number in (SIGTERM, SIGKILL):
            try:
                os.killpg(proc.pid, number)
            except ProcessLookupError:
                pass
            if number == SIGTERM:
                await asyncio.sleep(.1)
        await proc.communicate()
        raise
    return sp.CompletedProcess(argv, proc.returncode, stdout, stderr)


class TaskInput:
    """One producer, one task, and no access to the session's command stdin."""

    def __init__(self, result, broker):
        self.result = Path(result)
        self.broker = broker
        self.fifo = self.result.with_name("stdin")
        self.reader = self.keeper = None
        self.attached = asyncio.Event()

    def release(self, name):
        fd = getattr(self, name)
        if fd is not None:
            setattr(self, name, None)
            os.close(fd)

    def launched(self):
        # The child now owns stdin. Keep only a writer until the remote
        # sender joins the FIFO, so the child cannot see premature EOF.
        self.release("reader")
        self.result.with_name("requested").touch()

    def writer_attached(self):
        self.release("keeper")
        self.attached.set()

    async def run(self, command, *, service):
        process = delivery = attachment = None
        detached = False
        self.fifo.parent.mkdir(mode=0o700, exist_ok=True)
        os.mkfifo(self.fifo, 0o600)
        try:
            self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
            self.keeper = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(self.reader, True)
            process = asyncio.create_task(command(self.reader, self.launched))
            delivery = asyncio.create_task(wait_copy(self.result, broker=self.broker, input_pipe=self))
            await asyncio.wait((process, delivery), return_when=asyncio.FIRST_COMPLETED)
            if process.done():
                result = process.result()
                if result is not None and result.returncode:
                    return result
                if not delivery.done():
                    if service:
                        attachment = asyncio.create_task(self.attached.wait())
                        await asyncio.wait((delivery, attachment), return_when=asyncio.FIRST_COMPLETED)
                        if not delivery.done():
                            # A ready service may consume input only once the main
                            # command connects. The SSH helper now owns the
                            # unnamed pipe's writer; do not wait for it to see EOF.
                            detached = True
                            return result
                    else:
                        try:
                            await asyncio.wait_for(asyncio.shield(delivery), 3)
                        except TimeoutError:
                            # A successful finite command can intentionally stop
                            # reading before its writer has even started.
                            return result
            transferred = await delivery
            if transferred.returncode:
                return transferred
            return await process
        finally:
            try:
                if not detached and (delivery is None or not delivery.done()):
                    self.result.with_name("cancelled").touch()
            finally:
                self.release("reader")
                self.release("keeper")
                self.fifo.unlink(missing_ok=True)
                tasks = [task for task in (process, delivery, attachment) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class Lifecycle:
    def __init__(self):
        self.setup = None
        self.command = None
        self.interrupted = 0
        self.environment = None
        self.nodes = {}
        self.began = set()
        self.services = {}
        self.setup_output = None

    def write_setup_output(self, data):
        if self.setup_output is not None:
            self.setup_output.append(data)
        elif isinstance(data, bytes):
            sys.stderr.buffer.write(data)
        else:
            print(data, file=sys.stderr, flush=True)

    def flush_setup_output(self):
        deferred, self.setup_output = self.setup_output, None
        if deferred:
            try:
                fd = sys.stderr.fileno()
                for data in deferred:
                    if isinstance(data, str):
                        data = (data + "\n").encode(sys.stderr.encoding or "utf-8",
                                                     sys.stderr.errors or "backslashreplace")
                    pending = memoryview(data)
                    while pending:
                        written = os.write(fd, pending)
                        if not written:
                            return
                        pending = pending[written:]
            except (OSError, ValueError):
                # Do not poison Python's buffered stderr on a closed sink:
                # it can replace an already known exit code at shutdown.
                pass

    def interrupt(self, number, _):
        if not self.interrupted:
            self.interrupted = number
            if self.setup is not None:
                self.setup.cancel()
            elif self.command is not None and number != SIGINT:
                self.command.send_signal(number)

    async def prepare(self, run, plan, drive, drive_config=None):
        self.setup = asyncio.current_task()
        try:
            if self.interrupted:
                return 128 + self.interrupted
            return await setup_all(run, plan, drive, self, drive_config)
        except asyncio.CancelledError:
            if not self.interrupted:
                raise
            return 128 + self.interrupted
        finally:
            self.setup = None

    async def start(self, name, step, stdin, on_start=None):
        argv = step["start"]
        service = sp.Popen(argv, env=self.environment, stdin=stdin,
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)
        self.services[name] = service
        if on_start:
            on_start()
        probe = step.get("ready")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        # Give an immediate exit a chance to become observable even without a probe.
        await asyncio.sleep(.05)
        while service.poll() is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(f"{step['description']} timed out waiting for readiness")
            if not probe:
                return
            attempt = asyncio.create_task(command_result(probe, self.environment, timeout=remaining))
            try:
                while service.poll() is None and not attempt.done():
                    await asyncio.wait((attempt,), timeout=.05)
                if service.poll() is not None:
                    break
                try:
                    result = attempt.result()
                except TimeoutError:
                    pass
                else:
                    if not result.returncode:
                        return
            finally:
                if not attempt.done():
                    attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await asyncio.sleep(min(.1, max(deadline - loop.time(), 0)))
        raise RuntimeError(f"{step['description']} exited before readiness (exit {service.returncode})")

    def check_services(self):
        for name, service in self.services.items():
            if service.poll() is not None:
                raise RuntimeError(f"{self.nodes[name][0]['description']} exited before the main command")

    def cleanup(self, drive):
        held = set()
        failed = False

        def hold(name):
            if name not in held:
                held.add(name)
                for dep in self.nodes[name][1]:
                    hold(dep)

        archives = {}
        for name in self.began:
            if result := self.nodes[name][0].get("copy_result"):
                try:
                    Path(result).with_name("cancelled").touch()
                    archives[name] = result
                except OSError as exc:
                    report("VM left running", f"Could not cancel copy for {self.nodes[name][0]['description']}: "
                           f"{exc.strerror}.")
                    hold(name)
                    failed = True
        deadline = time.monotonic() + 5
        for name, result in archives.items():
            try:
                idle = quiesce_archive(result, deadline)
            except OSError:
                idle = False
            if not idle:
                report("VM left running", f"Copy for {self.nodes[name][0]['description']} did not stop.")
                hold(name)
                failed = True

        for name in reversed(self.nodes):
            if name not in self.began or name in held:
                continue
            step, _ = self.nodes[name]
            try:
                ok = True
                if hook := step.get("teardown"):
                    ok = sp.call(hook, env=self.environment, stdin=sp.DEVNULL, stdout=sys.stderr) == 0
                if ok and (service := self.services.get(name)) is not None:
                    ok = stop_process(service)
                if ok and (target := step.get("mount_target")) is not None:
                    ok = drive.detach(target)
                if ok:
                    continue
                report("VM left running", f"Teardown of {step['description']} failed.")
            except OSError as exc:
                report("VM left running", f"Teardown of {step['description']} failed: {exc.strerror}.")
            failed = True
            hold(name)
        preserve = {Path(step["mount_target"]) for name, (step, _) in self.nodes.items()
                    if name in held and "mount_target" in step}
        released = drive.close(preserve, keep_daemon="rcd" in held) if drive else True
        return released and not failed


async def setup_all(run, plan, drive, lifecycle=None, drive_config=None):
    lifecycle = lifecycle or Lifecycle()
    tasks = {}

    def submit(name, description, command, *dependencies, step=None):
        step = step if step is not None else {}
        if "description" not in step:
            step = {**step, "description": description}
        lifecycle.nodes[name] = (step, dependencies)
        reserve = getattr(run, "reserve", None)
        if reserve is not None:
            reserve(description)

        async def execute():
            results = await asyncio.gather(*(tasks[dep] for dep in dependencies))
            if any(result for result in results):
                return 1
            lifecycle.began.add(name)
            result = await run(description, command)
            if result is not None and result.returncode:
                if name in ("fuse", "rclone"):
                    # Built-in installers are managed commands, not profile
                    # hooks. Their free-form output may contain internal traces.
                    if plan.get("debug"):
                        text = b"\n".join((result.stdout or b"", result.stderr or b"")).decode(errors="replace")
                        for detail in safe_subprocess_details(text):
                            lifecycle.write_setup_output(detail)
                else:
                    if result.stdout:
                        lifecycle.write_setup_output(result.stdout)
                    if result.stderr:
                        lifecycle.write_setup_output(result.stderr)
                if name != "copy":  # The local transfer reports its concrete error.
                    lifecycle.write_setup_output(outcome("Setup failed", f"{description} (exit {result.returncode}).",
                                                         color=_ONCOLAB_COLOR))
                return result.returncode
            return 0
        tasks[name] = asyncio.create_task(execute())

    copy_result = plan.get("copy_result")
    copy_supplies_workdir = plan.get("copy_supplies_workdir", False)
    if copy_result:
        async def copy():
            result = await wait_copy(copy_result)
            if copy_supplies_workdir and not result.returncode:
                enter_workdir(plan)
            return result
        submit("copy", "Copying files…", copy, step={"copy_result": copy_result})

    drive_tasks = []
    bash = ["bash", "-eo", "pipefail", "-c"]
    if drive:
        drive.environment = drive_environment(lifecycle.environment if lifecycle.environment is not None else os.environ)
        workdir_ready = ("copy",) if copy_supplies_workdir else ()

        def install(script):
            return partial(command_result, [*bash, script], drive.environment)

        submit("fuse", "Preparing FUSE…", install(FUSE), *workdir_ready)
        submit("rclone", "Preparing rclone…", install(RCLONE), *workdir_ready)
        drive_dependencies = ("fuse", "rclone")
        if drive_config is not None:
            async def receive_config():
                await drive_config

            submit("drive-config", "Receiving Drive config…", receive_config)
            drive_dependencies += ("drive-config",)
        submit("rcd", "Starting Drive mounts…", drive.start, *drive_dependencies)
        drive_tasks.extend(("fuse", "rclone", "rcd"))
        setup_mounts = {step.get("mount") for step in plan["setup"].values()}
        for name, mount in plan["mounts"].items():
            if name in setup_mounts:
                continue
            async def attach(mount=mount):
                await drive.attach(mount)
            submit(name, f"Mounting {name.removeprefix('mounts.')}…", attach, "rcd",
                   step={"mount_target": mount["target"]})
            drive_tasks.append(name)

    for name, step in plan["setup"].items():
        dependencies = [f"setup:{dep}" for dep in step["needs"]]
        if copy_supplies_workdir:
            dependencies.append("copy")
        key = f"setup:{name}"
        if mount_key := step.get("mount"):
            mount = plan["mounts"][mount_key]

            async def command(mount=mount):
                await drive.attach(mount)

            dependencies.append("rcd")
            drive_tasks.append(key)
            step = {**step, "mount_target": mount["target"]}
        elif lifecycle.environment is not None or {"copy_result", "stdin_result", "start"} & step.keys():
            async def command(step=step, key=key):
                broker = Path(plan.get("jobdir", "/dev/null")) / "broker-pid"
                if result_path := step.get("copy_result"):
                    Path(result_path).with_name("requested").touch()
                    result = await wait_copy(result_path, broker=broker)
                    if result.returncode:
                        return result

                async def launch(stdin=sp.DEVNULL, on_start=None):
                    if "start" in step:
                        await lifecycle.start(key, step, stdin, on_start)
                        return None
                    if argv := step.get("argv"):
                        return await command_result(argv, lifecycle.environment, stdin, on_start=on_start)
                    return None

                if input_result := step.get("stdin_result"):
                    service = "start" in step
                    result = await TaskInput(input_result, broker).run(launch, service=service)
                    if service and result is None and lifecycle.services[key].poll() is not None:
                        raise RuntimeError(f"{step['description']} exited while opening task input")
                    return result
                return await launch()
        else:
            command = step.get("argv", lambda: None)
        submit(key, step["description"], command, *dependencies, step=step)

    try:
        if drive:
            try:
                mount_results = ()
                try:
                    mount_results = await asyncio.gather(*(tasks[name] for name in drive_tasks),
                                                         return_exceptions=True)
                finally:
                    if drive.socket.exists():
                        # Allow the initial OAuth refresh to save to the private tmpfs
                        # file; an empty path makes rclone retry a failed save.
                        try:
                            await drive.rc("config/setpath", path="/dev/null")
                        except RuntimeError:
                            if not (any(mount_results) or lifecycle.interrupted):
                                raise
                            lifecycle.write_setup_output(outcome("Drive cleanup incomplete",
                                                                 "Could not detach rclone from its staging path.",
                                                                 color=_ONCOLAB_COLOR))
            finally:
                drive.seal()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return next((result for result in results if result), 0)


def send_setup_output(gate, lifecycle):
    messages, lifecycle.setup_output = lifecycle.setup_output or [], None
    for data in messages:
        if isinstance(data, str):
            data = (data + "\n").encode("utf-8", "backslashreplace")
        for offset in range(0, len(data), 65536):
            gate.send("diag", base64.b64encode(data[offset:offset + 65536]).decode())
    gate.send("end-output")


async def pipeline_setup(run, lifecycle, plan, drive, gate):
    accepted = asyncio.create_task(gate.accept())
    stream, transport = await stdin_packets()
    staged_config = None
    loop = asyncio.get_running_loop()
    watching = None
    descriptor = None
    try:
        setup_env, command_env = json.loads(await stream.readline())
        lifecycle.environment = os.environ | setup_env

        async def receive_drive():
            raw = base64.b64decode(json.loads(await stream.readline()), validate=True)
            with os.fdopen(os.open(drive.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(raw)

        if drive is not None:
            staged_config = asyncio.create_task(receive_drive())
        counter = 0

        async def event_run(label, invoke):
            nonlocal counter
            key = counter
            counter += 1
            gate.send("start", key, label, time.monotonic())
            try:
                return await run(label, invoke)
            finally:
                gate.send("done", key)

        async def detect_disconnect():
            nonlocal descriptor
            try:
                await accepted
            except (OSError, TimeoutError):
                lifecycle.interrupt(SIGTERM, None)
                return
            descriptor = gate.peer.fileno()

            def disconnected():
                # The command channel sends nothing before setup ends.
                if not os.read(descriptor, 1):
                    loop.remove_reader(descriptor)
                    lifecycle.interrupt(SIGTERM, None)

            loop.add_reader(descriptor, disconnected)

        watching = asyncio.create_task(detect_disconnect())
        status = await lifecycle.prepare(event_run, plan, drive, staged_config)
        await accepted
        inherited = {key: value for key, value in os.environ.items()
                     if key in ("PATH", "COLAB_DRIVE") or key.startswith("ONCOLAB_MOUNT_")}
        return status, inherited | setup_env | command_env
    finally:
        transport.close()
        if watching is not None:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
        if descriptor is not None:
            loop.remove_reader(descriptor)
        if staged_config is not None:
            if not staged_config.done():
                staged_config.cancel()
            await asyncio.gather(staged_config, return_exceptions=True)
        if not accepted.done():
            accepted.cancel()
        await asyncio.gather(accepted, return_exceptions=True)


def main():
    global _ONCOLAB_COLOR
    directory, color, raw_plan, *command = sys.argv[1:]
    _ONCOLAB_COLOR = color == "1"
    plan = json.loads(raw_plan)
    owner = plan.get("pipeline_role") == "setup"
    control_dir = Path(plan["pipeline_control"]).parent if owner else None
    gate = None
    owner_reports = None
    saved_stderr = None
    sent_result = False
    command_stopped = True
    live = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    handoff = plan.get("terminal_handoff")

    def takeover(*, erase=True):
        nonlocal handoff
        if handoff:
            handoff = None
            print("\x1b[A\r" + ("\x1b[K" if erase else ""), end="", flush=True)

    os.environ["PATH"] = str(Path.home() / ".local/bin") + ":" + os.environ["PATH"]
    drive = None
    jobdir = Path(plan["jobdir"]) if "jobdir" in plan else None
    setup_env, command_env = {}, {}
    lifecycle = Lifecycle()
    status = 1
    previous = {n: signal(n, lifecycle.interrupt) for n in (SIGINT, SIGHUP, SIGTERM)}
    try:
        if owner:
            staging_directories(plan["pipeline_dirs"])
            control_dir.mkdir(mode=0o700)
            gate = SetupGate(plan["pipeline_control"])
            owner_reports = TemporaryFile(dir=control_dir)
            saved_stderr = os.dup(sys.stderr.fileno())
            os.dup2(owner_reports.fileno(), sys.stderr.fileno())
            sys.stdout.buffer.write(SETUP_LISTENING)
            sys.stdout.buffer.flush()
            lifecycle.setup_output = []
        if plan["mounts"]:
            path = Path(directory)
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise RuntimeError("Drive staging directory is not private")
            drive = DriveMounts(directory)
            if not owner:
                (path / "rclone.conf").rename(drive.config)
            for name, mount in plan["mounts"].items():
                var = name.removeprefix("mounts.").upper().replace("-", "_")
                os.environ[f"ONCOLAB_MOUNT_{var}"] = mount["target"]
        if jobdir:
            st = jobdir.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise RuntimeError("Job staging directory is not private")
            if plan.get("has_env") and not owner:
                claimed = jobdir / "active-env.json"
                try:
                    (jobdir / "env.json").rename(claimed)
                except FileNotFoundError:
                    raise RuntimeError("Staged environment is missing; refusing to run without it") from None
                try:
                    with claimed.open() as file:
                        claimed.unlink()
                        setup_env, command_env = json.load(file)
                finally:
                    claimed.unlink(missing_ok=True)
        if not plan.get("copy_supplies_workdir"):
            enter_workdir(plan)
        work = plan["mounts"].get("mounts.work")
        if work:
            os.environ["COLAB_DRIVE"] = work["target"]
        lifecycle.environment = os.environ | setup_env
        options = {"labels": plan["progress_labels"]} if "progress_labels" in plan else {}
        # Replaying hook diagnostics while the live renderer owns the
        # cursor can overwrite their first line (or leave stale timer text).
        if live:
            lifecycle.setup_output = []
        if owner:
            status, command_env = progress(lambda run: pipeline_setup(run, lifecycle, plan, drive, gate),
                                           False, False, capture=True)
            send_setup_output(gate, lifecycle)
        else:
            try:
                status = progress(lambda run: lifecycle.prepare(run, plan, drive), live, color == "1",
                                  capture=True, idle=handoff, takeover=lambda: takeover(erase=False), **options)
            finally:
                lifecycle.flush_setup_output()
        if lifecycle.interrupted:
            status = 128 + lifecycle.interrupted
        elif not status:
            lifecycle.check_services()
            argv = [*plan["entrypoint"], *command]
            try:
                environment = {name: value for name, value in (lifecycle.environment | command_env).items()
                               if value is not None}
                if owner:
                    gate.send("run", os.getcwd(), command_env)
                    sent_result = True
                    status = gate.receive_status(lambda: lifecycle.interrupted)
                else:
                    takeover()
                    with sp.Popen(argv, env=environment) as cmd:
                        lifecycle.command = cmd
                        code = cmd.wait()
                    status = code if code >= 0 else 128 - code
            except OSError as exc:
                if owner:
                    raise
                report("Command could not start", f"{argv[0]}: {exc.strerror}.")
                status = 127 if isinstance(exc, FileNotFoundError) else 126
    except (OSError, RuntimeError) as exc:
        takeover()
        if owner and isinstance(exc, CommandStateUnknown):
            command_stopped = False
            report("VM left running", str(exc))
        else:
            report("Remote preparation failed", str(exc))
        status = status or 1
    except Exception as exc:
        takeover()
        internal_error("remote preparation", exc, plan.get("debug"))
        status = status or 1
    finally:
        lifecycle.command = None
        if gate is not None and gate.peer is not None and not sent_result:
            try:
                if lifecycle.setup_output is not None:
                    send_setup_output(gate, lifecycle)
                gate.send("failed")
            except (OSError, ValueError):
                pass
        try:
            released = command_stopped and lifecycle.cleanup(drive)
            if jobdir and command_stopped:
                shutil.rmtree(jobdir, ignore_errors=True)
        except OSError as exc:
            report("VM left running", f"Cleanup failed: {exc.strerror}.")
            released = False
        except Exception as exc:
            internal_error("remote cleanup; leaving the VM running", exc, plan.get("debug"))
            released = False
    if not released and (hint := plan.get("recovery_hint")):
        report("Check VM state", f"{hint}.")
    final_status = (status if status >= 0 else 128 - status) if released else 125
    if gate is not None:
        try:
            if owner_reports is not None:
                sys.stderr.flush()
                os.dup2(saved_stderr, sys.stderr.fileno())
                os.close(saved_stderr)
                owner_reports.seek(0)
            if gate.peer is not None:
                deadline = time.monotonic() + 3
                while owner_reports is not None and (data := owner_reports.read(65536)):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    gate.peer.settimeout(left)
                    gate.send("diag", base64.b64encode(data).decode())
                gate.peer.settimeout(max(.05, deadline - time.monotonic()))
                gate.send("status", final_status)
            elif owner_reports is not None:
                shutil.copyfileobj(owner_reports, sys.stderr.buffer)
        except OSError:
            pass
        finally:
            gate.close()
            if owner_reports is not None:
                owner_reports.close()
    if control_dir is not None:
        shutil.rmtree(control_dir, ignore_errors=True)
    try:
        if released:
            unassign()
    finally:
        for number, handler in previous.items():
            signal(number, handler)
    return final_status


def internal_error(operation, exc, debug):
    report("oncolab failed", f"Unexpected internal error during {operation}. Use --debug for details.")
    if debug:
        from traceback import extract_tb
        print(type(exc).__name__, file=sys.stderr)
        for frame in extract_tb(exc.__traceback__):
            print(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        internal_error("remote execution", exc, globals().get("_ONCOLAB_DEBUG", False))
        raise SystemExit(1) from None
