import asyncio
import json
import os
import shutil
import stat
import subprocess as sp
import sys
from pathlib import Path
from signal import SIGINT, signal


def unassign():
    sp.Popen([sys.executable, "-c", r'''
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
'''], start_new_session=True, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL)

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

    async def rc(self, method, **parameters):
        process = await asyncio.create_subprocess_exec(
            "rclone", "--config", "/dev/null", "rc", "--unix-socket", str(self.socket),
            method, "--json", json.dumps(parameters), stdout=sp.PIPE, stderr=sp.PIPE,
        )
        await process.communicate()
        if process.returncode:
            raise RuntimeError(f"rclone {method} failed (exit {process.returncode})")

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
        await self.rc("mount/mount", fs=mount["source"], mountPoint=str(target),
                      vfsOpt={"ReadOnly": mount["read_only"]})
        self.attached.append(target)

    def seal(self):
        try:
            shutil.rmtree(self.directory)
        except FileNotFoundError:
            pass

    def close(self):
        self.seal()
        failures = [path for path in reversed(self.attached) if sp.call(["umount", "--", path])]
        if failures:
            for path in failures:
                print(f"oncolab: could not unmount {path}; leaving the VM running", file=sys.stderr)
            return False
        if self.server is not None:
            self.server.terminate()
            self.server.wait()
        return True


async def setup_all(run, plan, drive):
    tasks = {}

    def submit(name, description, command, *dependencies):
        async def execute():
            results = await asyncio.gather(*(tasks[dep] for dep in dependencies))
            if any(result for result in results):
                return 1
            result = await run(description, command)
            if result is not None and result.returncode:
                if result.stdout:
                    sys.stderr.buffer.write(result.stdout)
                if result.stderr:
                    sys.stderr.buffer.write(result.stderr)
                print(f"oncolab: {description} failed (exit {result.returncode})", file=sys.stderr)
                return result.returncode
            return 0
        tasks[name] = asyncio.create_task(execute())

    bash = ["bash", "-eo", "pipefail", "-c"]
    if drive:
        submit("fuse", "Preparing FUSE…", [*bash, FUSE])
        submit("rclone", "Preparing rclone…", [*bash, RCLONE])
        submit("rcd", "Starting Drive mounts…", drive.start, "fuse", "rclone")
        for name, mount in plan["mounts"].items():
            async def attach(mount=mount):
                await drive.attach(mount)
            submit(name, f"Mounting {name.removeprefix('mounts.')}…", attach, "rcd")

    names = list(tasks)
    for name, step in plan["setup"].items():
        submit(f"setup:{name}", step["description"], step["argv"],
               *(dep if dep.startswith("mounts.") else f"setup:{dep}" for dep in step["depends"]))

    if drive:
        try:
            mount_results = await asyncio.gather(*(tasks[name] for name in names), return_exceptions=True)
            if drive.socket.exists():
                # Allow the initial OAuth refresh to save to the private tmpfs
                # file; an empty path makes rclone retry a failed save.
                try:
                    await drive.rc("config/setpath", path="/dev/null")
                except RuntimeError:
                    if not any(mount_results):
                        raise
                    print("oncolab: could not detach rclone from its staging path", file=sys.stderr)
        finally:
            drive.seal()
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
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
    status = 1
    signal(SIGINT, lambda *_: None)
    try:
        os.chdir("/content")
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
        status = progress(lambda run: setup_all(run, plan, drive), live, color == "1", capture=True)
        if not status:
            argv = [*plan["entrypoint"], *command]
            try:
                code = sp.call(argv)
                status = code if code >= 0 else 128 - code
            except OSError as exc:
                print(f"oncolab: {argv[0]}: {exc.strerror}", file=sys.stderr)
                status = 127 if isinstance(exc, FileNotFoundError) else 126
    except (OSError, RuntimeError) as exc:
        print(f"oncolab: {exc}", file=sys.stderr)
    finally:
        try:
            released = drive is None or drive.close()
        except OSError as exc:
            print(f"oncolab: cleanup failed; leaving the VM running: {exc.strerror}", file=sys.stderr)
            released = False
        if released:
            unassign()
    return (status if status >= 0 else 128 - status) if released else 125


if __name__ == "__main__":
    raise SystemExit(main())
