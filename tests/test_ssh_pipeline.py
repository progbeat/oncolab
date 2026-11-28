import errno
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from oncolab.ssh_pipeline import SETUP_LISTENING, packet


DRIVE_FIXTURE = '''
import sys
from pathlib import Path
from oncolab import remote

root = Path(sys.argv[1]).parent
class FakeDrive(remote.DriveMounts):
    async def start(self):
        assert self.config.read_bytes() == b"private configuration"
        assert self.config.stat().st_mode & 0o077 == 0
        (root / "mount-started").touch()

    async def attach(self, mount):
        pass

    def close(self, *args, **kwargs):
        (root / "owner-cleaned").touch()
        return True

real = remote.command_result

async def run(argv, env=None, *args, **kwargs):
    if len(argv) > 2 and argv[-1] in (remote.FUSE, remote.RCLONE):
        assert env["SETUP_VALUE"] == "early"
        (root / "prerequisites").touch()
        return None
    return await real(argv, env, *args, **kwargs)

accept = remote.SetupGate.accept
async def tracked_accept(self):
    await accept(self)
    (root / "control-attached").touch()

remote.SetupGate.accept = tracked_accept
remote.DriveMounts = FakeDrive
remote.command_result = run
remote.unassign = lambda: (root / "unassigned").touch()
raise SystemExit(remote.main())
'''


def wait_file(path):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    if not path.exists():
        raise AssertionError(f"{path.name} did not appear")


class ParallelSSH(unittest.TestCase):
    def test_setup_precedes_drive_config_and_command_waits(self):
        self.run_pipeline()

    def test_command_disconnect_cancels_setup_and_releases_vm(self):
        self.run_pipeline(disconnect=True)

    def test_failed_setup_skips_command_and_unassigns(self):
        self.run_pipeline(setup_fails=True)

    def run_pipeline(self, *, disconnect=False, setup_fails=False):
        with tempfile.TemporaryDirectory(prefix="ocp-", dir="/tmp") as directory:
            root = Path(directory)
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.bind(str(root / "probe"))
                except OSError as exc:
                    if exc.errno in (errno.EPERM, errno.EACCES):
                        self.skipTest("sandbox prevents Unix socket binding")
                    raise
            drive = str(root / "drive")
            control = str(root / "gate" / "control.sock")
            mark = 'from pathlib import Path; Path("independent").touch()'
            if setup_fails:
                mark += '; raise SystemExit(7)'
            config = {
                "pipeline_role": "setup", "pipeline_control": control, "debug": True,
                "pipeline_dirs": {drive: {}}, "entrypoint": [], "workdir": directory,
                "mounts": {"mounts.work": {"target": "/mock/drive"}},
                "setup": {"independent": {"description": "Independent setup", "needs": [],
                                           "argv": [sys.executable, "-c", mark]}},
            }
            owner = subprocess.Popen(
                [sys.executable, "-c", DRIVE_FIXTURE, drive, "0", json.dumps(config), "unused"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            client = None
            try:
                self.assertTrue(select.select([owner.stdout], [], [], 10)[0])
                self.assertEqual(owner.stdout.readline(), SETUP_LISTENING)
                spawn = (
                    'import os, pathlib, sys; '
                    'assert pathlib.Path("mount-started").exists(); '
                    'assert not pathlib.Path("owner-cleaned").exists(); '
                    'assert os.environ["SETUP_VALUE"] == "early"; '
                    'assert os.environ["COMMAND_VALUE"] == "late"; '
                    'assert os.environ["COLAB_DRIVE"] == "/mock/drive"; '
                    'print(sys.stdin.read(), end=""); raise SystemExit(255)'
                )
                client = subprocess.Popen(
                    [sys.executable, "-m", "oncolab.remote", control, "0", '{"pipeline_role":"command"}',
                     sys.executable, "-c", spawn],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                owner.stdin.write(packet({"SETUP_VALUE": "early"}, {"COMMAND_VALUE": "late"}))
                owner.stdin.flush()
                wait_file(root / "independent")
                wait_file(root / "prerequisites")
                self.assertFalse((root / "mount-started").exists())
                self.assertIsNone(client.poll())
                if disconnect:
                    wait_file(root / "control-attached")
                    client.kill()
                    client.communicate(timeout=5)
                    owner.wait(timeout=8)
                    self.assertTrue((root / "owner-cleaned").exists())
                    self.assertTrue((root / "unassigned").exists())
                    self.assertFalse((root / "mount-started").exists())
                    return
                owner.stdin.write(b'"cHJpdmF0ZSBjb25maWd1cmF0aW9u"\n')
                owner.stdin.flush()
                out, err = client.communicate(b"untouched stdin\n", timeout=15)
                if setup_fails:
                    self.assertEqual((client.returncode, out), (7, b""))
                    self.assertIn(b"[Setup failed] Independent setup (exit 7).", err)
                else:
                    self.assertEqual((client.returncode, out, err), (255, b"untouched stdin\n", b""))
                _, err = owner.communicate(timeout=10)
                self.assertEqual((owner.returncode, err), (7 if setup_fails else 255, b""))
                self.assertTrue((root / "owner-cleaned").exists())
                self.assertTrue((root / "unassigned").exists())
            finally:
                for process in (client, owner):
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()
