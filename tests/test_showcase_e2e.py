"""Run the installed CLI and remote runner together without allocating a VM."""
import os
from pathlib import Path

from allocator_fixture import fake_colab
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


SSH = r'''
import os
import shlex
import sys

args = sys.argv[1:]
if "-O" in args:
    raise SystemExit(0)
ram = os.environ["FAKE_RAM"] + "/oncolab-"
command = shlex.split(args[-1].replace("/dev/shm/oncolab-", ram))
if "-M" in args:
    assert "127.0.0.1:18080:127.0.0.1:8765" in args
    for i, option in enumerate(args):
        if option == "-L" and args[i+1].startswith("/"):
            local, remote = args[i+1].split(":", 1)
            os.symlink(remote.replace("/dev/shm/oncolab-", ram), local)
    if "-N" in args:
        assert not sys.stdin.buffer.read()
        raise SystemExit(0)
    # The bootstrap's private JSON also contains the remote directory paths.
    import subprocess
    data = sys.stdin.buffer.read().replace(b"/dev/shm/oncolab-", os.fsencode(ram))
    raise SystemExit(subprocess.run([sys.executable, *command[1:]], input=data).returncode)
if command[:4] == ["uv", "run", "--no-project", "python3"]:
    if os.environ.get("TEST_OWNER_UV_MISSING") and '"pipeline_role": "setup"' in command[-1]:
        print("bash: line 1: uv: command not found", file=sys.stderr)
        raise SystemExit(127)
    command = command[3:]
assert command[0] == "python3"
os.execv(sys.executable, [sys.executable, *command[1:]])
'''


class Showcase(unittest.TestCase):
    def test_task_files_and_teardown_use_profile_env_with_cli_override(self):
        self.run_case(streamed=False)

    def test_task_input_and_teardown_use_profile_env_without_consuming_main_stdin(self):
        self.run_case(streamed=True)

    def test_missing_uv_is_a_setup_failure_and_releases_the_vm(self):
        self.run_case(streamed=False, missing_uv=True)

    def run_case(self, *, streamed, missing_uv=False):
        executable = shutil.which("oncolab")
        self.assertIsNotNone(executable)
        with tempfile.TemporaryDirectory(prefix="oc-e2e-", dir="/tmp") as directory:
            root = Path(directory).resolve()
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.bind(str(root / "probe"))
            except PermissionError:
                self.skipTest("sandbox does not allow Unix sockets")
            work, fake_ram = root / "vm", root / "ram"
            fake_ram.mkdir()
            fake_colab(root)
            ssh = root / "ssh"
            ssh.write_text(f"#!{sys.executable}\n{SSH}")
            ssh.chmod(0o755)
            # Task stdin writes a file before the dependent copy is materialized.
            (root / "prepare.sh").write_text('test "$VALUE" = shared\nprintf setup > prepared\n')
            (root / "data.txt").write_text("copied")
            profile = root / "test.toml"
            prepare = ('copy = "prepare.sh:-"\nrun = ["bash", "-e", "-o", "pipefail", "-s"]' if streamed
                       else """run = 'test "$VALUE" = shared; printf setup > prepared'""")
            profile.write_text(f'''workdir = "{work}"
ports = ["18080:8765"]
entrypoint = ["sh", "-ec", 'test "$SHARED" = profile; exec "$@"', "entrypoint"]
[env]
VALUE = "shared"
SHARED = "profile"
[setup.transfer]
needs = ["prepare"]
copy = [{{ source = "data.txt", target = "data", chmod = 0o640 }}]
run = 'test "$(cat prepared)" = setup'
teardown = 'test "$VALUE" = shared; printf teardown > done'
[setup.prepare]
{prepare}
''')
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "FAKE_RAM": str(fake_ram),
                   "XDG_CONFIG_HOME": str(root), "NO_COLOR": "1"}
            env.pop("ONCOLAB_PARALLEL_SETUP", None)
            if missing_uv:
                env.update(TEST_OWNER_UV_MISSING="1", TEST_RELEASE_MARKER=str(root / "released"))
            result = subprocess.run([executable, "--config", str(profile), "-e", "VALUE=override", "--",
                                     "sh", "-ec", 'test "$VALUE" = override; test "$SHARED" = profile; test -z "$(find "$FAKE_RAM" -name "*env.json")"; cat'],
                                    env=env, input="main stdin\n", text=True, capture_output=True, timeout=20)
            if missing_uv:
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertIn("[Setup could not start] uv is not available on the Colab VM.", result.stderr)
                self.assertNotIn("[SSH connection failed]", result.stderr)
                self.assertTrue((root / "released").exists())
                return
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "main stdin\n")
            self.assertEqual((work / "data").read_text(), "copied")
            self.assertEqual((work / "data").stat().st_mode & 0o777, 0o640)
            self.assertEqual((work / "done").read_text(), "teardown")
            self.assertFalse((work / "prepare.sh").exists())
