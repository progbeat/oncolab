import asyncio
import json
import os
import shutil
import stat
import subprocess as sp
import sys
from contextlib import ExitStack
from pathlib import Path
from signal import SIGHUP, SIGINT, SIGKILL, SIGTERM, signal


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

class DriveMounts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.config = self.directory / "rclone.conf"
        self.socket = self.directory / "rc.sock"
        self.attached = []
        self.server = None
        self.uncertain = False

    async def rc(self, method, **parameters):
        command = ["rclone", "--config", "/dev/null", "rc", "--unix-socket", str(self.socket),
                   method, "--json", json.dumps(parameters)]
        try:
            result = await command_result(command, None, timeout=45)
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
            stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True,
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
                print(f"oncolab: could not unmount {target}; leaving the VM running", file=sys.stderr)
                return False
            self.attached.remove(target)
        return True

    def close(self, preserve=(), *, keep_daemon=False):
        self.seal()
        results = [self.detach(path) for path in reversed(self.attached[:]) if path not in preserve]
        if self.uncertain:
            print("oncolab: Drive mount state is uncertain; leaving the VM and daemon running", file=sys.stderr)
        if not all(results) or self.attached or keep_daemon or self.uncertain:
            return False
        if self.server is not None:
            if not stop_process(self.server):
                return False
        return True


async def wait_copy(result, *, start_timeout=30, broker=None):
    path = Path(result)
    pid_path = path.with_name("copy-pid")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + start_timeout

    def read(path):
        try:
            return path.read_text()
        except FileNotFoundError:
            return ""

    while True:
        status = read(path)
        if status in ("ok", "failed"):
            return sp.CompletedProcess([], int(status != "ok"), None, None)

        pid_text = read(pid_path)
        try:
            broker_text = read(broker) if broker else ""
            if broker_text.endswith("\n"):
                os.kill(int(broker_text), 0)
            if pid_text.endswith("\n"):
                os.kill(int(pid_text), 0)
            elif not broker_text and loop.time() >= deadline:
                raise ProcessLookupError
        except ProcessLookupError:
            if read(path) in ("ok", "failed"):
                continue  # Receiver may have published just before exiting.
            return sp.CompletedProcess([], 1, None, b"oncolab: copy receiver exited or did not start\n")
        await asyncio.sleep(.10)


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
            print(f"oncolab: could not stop process {proc.pid}", file=sys.stderr)
            return False
    # A shell may exit before its process group. Do not leave descendants alive.
    try:
        os.killpg(proc.pid, SIGKILL)
    except ProcessLookupError:
        pass
    return True


async def command_result(argv, env, stdin=sp.DEVNULL, *, timeout=None):
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=stdin, stdout=sp.PIPE, stderr=sp.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.CancelledError, TimeoutError):
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


class Lifecycle:
    def __init__(self):
        self.setup = None
        self.command = None
        self.interrupted = 0
        self.environment = None
        self.nodes = {}
        self.began = set()
        self.services = {}

    def interrupt(self, number, _):
        if not self.interrupted:
            self.interrupted = number
            if self.setup is not None:
                self.setup.cancel()
            elif self.command is not None and number != SIGINT:
                self.command.send_signal(number)

    async def prepare(self, run, plan, drive):
        self.setup = asyncio.current_task()
        try:
            if self.interrupted:
                return 128 + self.interrupted
            return await setup_all(run, plan, drive, self)
        except asyncio.CancelledError:
            if not self.interrupted:
                raise
            return 128 + self.interrupted
        finally:
            self.setup = None

    async def start(self, name, step, stdin):
        argv = step["start"]
        service = sp.Popen(argv, env=self.environment, stdin=stdin,
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)
        self.services[name] = service
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
            try:
                result = await command_result(probe, self.environment, timeout=min(remaining, 2))
            except TimeoutError:
                pass
            else:
                if not result.returncode and service.poll() is None:
                    return
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
                print(f"oncolab: teardown of {step['description']} failed; leaving the VM running", file=sys.stderr)
            except OSError as exc:
                print(f"oncolab: teardown of {step['description']} failed: {exc.strerror}; leaving the VM running",
                      file=sys.stderr)
            failed = True
            hold(name)
        preserve = {Path(step["mount_target"]) for name, (step, _) in self.nodes.items()
                    if name in held and "mount_target" in step}
        released = drive.close(preserve, keep_daemon="rcd" in held) if drive else True
        return released and not failed


async def setup_all(run, plan, drive, lifecycle=None):
    lifecycle = lifecycle or Lifecycle()
    tasks = {}

    def submit(name, description, command, *dependencies, step=None):
        step = step if step is not None else {}
        if "description" not in step:
            step = {**step, "description": description}
        lifecycle.nodes[name] = (step, dependencies)

        async def execute():
            results = await asyncio.gather(*(tasks[dep] for dep in dependencies))
            if any(result for result in results):
                return 1
            lifecycle.began.add(name)
            result = await run(description, command)
            if result is not None and result.returncode:
                if result.stdout:
                    sys.stderr.buffer.write(result.stdout)
                if result.stderr:
                    sys.stderr.buffer.write(result.stderr)
                if name != "copy":  # The local transfer reports its concrete error.
                    print(f"oncolab: {description} failed (exit {result.returncode})", file=sys.stderr)
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
        submit("copy", "Copying files…", copy)

    drive_tasks = []
    bash = ["bash", "-eo", "pipefail", "-c"]
    if drive:
        submit("fuse", "Preparing FUSE…", [*bash, FUSE])
        submit("rclone", "Preparing rclone…", [*bash, RCLONE])
        submit("rcd", "Starting Drive mounts…", drive.start, "fuse", "rclone")
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
        dependencies = list(step["depends"])
        if copy_supplies_workdir and "copy" not in dependencies:
            dependencies.append("copy")
        dependencies = [dep if dep.startswith("mounts.") or (copy_result and dep == "copy")
                        else f"setup:{dep}" for dep in dependencies]
        key = f"setup:{name}"
        if mount_key := step.get("mount"):
            mount = plan["mounts"][mount_key]

            async def command(mount=mount):
                await drive.attach(mount)

            dependencies.append("rcd")
            drive_tasks.append(key)
            step = {**step, "mount_target": mount["target"]}
        elif lifecycle.environment is not None or "copy_result" in step or "start" in step:
            async def command(step=step, key=key):
                with ExitStack() as stack:
                    stdin = sp.DEVNULL
                    if result_path := step.get("copy_result"):
                        Path(result_path).with_name("requested").touch()
                        result = await wait_copy(result_path, broker=Path(plan["jobdir"]) / "broker-pid")
                        if result.returncode:
                            return result
                    if path := step.get("stdin"):
                        stdin = stack.enter_context(open(path, "rb"))
                        Path(path).unlink()
                    if "start" in step:
                        await lifecycle.start(key, step, stdin)
                        return None
                    if argv := step.get("argv"):
                        return await command_result(argv, lifecycle.environment, stdin)
                    return None
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
                            print("oncolab: could not detach rclone from its staging path", file=sys.stderr)
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


def main():
    directory, color, raw_plan, *command = sys.argv[1:]
    plan = json.loads(raw_plan)
    live = sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"
    os.environ["PATH"] = str(Path.home() / ".local/bin") + ":" + os.environ["PATH"]
    drive = None
    jobdir = Path(plan["jobdir"]) if "jobdir" in plan else None
    setup_env, command_env = {}, {}
    lifecycle = Lifecycle()
    status = 1
    previous = {n: signal(n, lifecycle.interrupt) for n in (SIGINT, SIGHUP, SIGTERM)}
    try:
        if not plan.get("copy_supplies_workdir"):
            enter_workdir(plan)
        if jobdir:
            st = jobdir.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise RuntimeError("Job staging directory is not private")
            env_path = jobdir / "env.json"
            if env_path.exists():
                values = json.loads(env_path.read_text())
                if isinstance(values, list):
                    setup_env, command_env = values
                else:
                    command_env = values
                env_path.unlink()
        if plan["mounts"]:
            path = Path(directory)
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise RuntimeError("Drive staging directory is not private")
            drive = DriveMounts(directory)
            for name, mount in plan["mounts"].items():
                var = name.removeprefix("mounts.").upper().replace("-", "_")
                os.environ[f"ONCOLAB_MOUNT_{var}"] = mount["target"]
        work = plan["mounts"].get("mounts.work")
        if work:
            os.environ["COLAB_DRIVE"] = work["target"]
        lifecycle.environment = os.environ | setup_env
        status = progress(lambda run: lifecycle.prepare(run, plan, drive), live, color == "1", capture=True)
        if lifecycle.interrupted:
            status = 128 + lifecycle.interrupted
        elif not status:
            lifecycle.check_services()
            argv = [*plan["entrypoint"], *command]
            try:
                environment = {name: value for name, value in (lifecycle.environment | command_env).items()
                               if value is not None}
                with sp.Popen(argv, env=environment) as cmd:
                    lifecycle.command = cmd
                    code = cmd.wait()
                status = code if code >= 0 else 128 - code
            except OSError as exc:
                print(f"oncolab: {argv[0]}: {exc.strerror}", file=sys.stderr)
                status = 127 if isinstance(exc, FileNotFoundError) else 126
    except (OSError, RuntimeError) as exc:
        print(f"oncolab: {exc}", file=sys.stderr)
    finally:
        lifecycle.command = None
        try:
            released = lifecycle.cleanup(drive)
            if jobdir:
                shutil.rmtree(jobdir, ignore_errors=True)
        except OSError as exc:
            print(f"oncolab: cleanup failed; leaving the VM running: {exc.strerror}", file=sys.stderr)
            released = False
        if released:
            unassign()
        for number, handler in previous.items():
            signal(number, handler)
    return (status if status >= 0 else 128 - status) if released else 125


if __name__ == "__main__":
    raise SystemExit(main())
