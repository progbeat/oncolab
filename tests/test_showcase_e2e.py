"""Run the installed CLI and remote runner together without allocating a VM."""
import os
from pathlib import Path
import shutil
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
command = shlex.split(args[-1].replace("/dev/shm/oncolab-", os.environ["FAKE_RAM"] + "/oncolab-"))
if "-M" in args:
    assert "127.0.0.1:18080:127.0.0.1:8765" in args
    # The bootstrap's private JSON also contains the remote directory paths.
    import subprocess
    data = sys.stdin.buffer.read().replace(b"/dev/shm/oncolab-", os.fsencode(os.environ["FAKE_RAM"] + "/oncolab-"))
    raise SystemExit(subprocess.run([sys.executable, *command[1:]], input=data).returncode)
if command[:4] == ["uv", "run", "--no-project", "python3"]:
    command = command[3:]
assert command[0] == "python3"
os.execv(sys.executable, [sys.executable, *command[1:]])
'''


class Showcase(unittest.TestCase):
    def test_task_input_and_teardown_use_profile_env_without_consuming_main_stdin(self):
        executable = shutil.which("oncolab")
        self.assertIsNotNone(executable)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            work, fake_ram = root / "vm", root / "ram"
            fake_ram.mkdir()
            colab, ssh = root / "colab", root / "ssh"
            colab.write_text("#!/bin/sh\nprintf \"[colab] Creating session 'showcase-test'...\\n\"\n")
            ssh.write_text(f"#!{sys.executable}\n{SSH}")
            for script in (colab, ssh):
                script.chmod(0o755)
            # Task stdin writes a file before the dependent copy is materialized.
            (root / "prepare.sh").write_text('test "$VALUE" = shared\nprintf setup > prepared\n')
            (root / "data.txt").write_text("copied")
            profile = root / "test.toml"
            profile.write_text(f'''workdir = "{work}"
ports = ["18080:8765"]
[env]
VALUE = "shared"
[setup.transfer]
needs = ["prepare"]
copy = [{{ source = "data.txt", target = "data", chmod = 0o640 }}]
run = 'test "$(cat prepared)" = setup'
teardown = 'test "$VALUE" = shared; printf teardown > done'
[setup.prepare]
copy = "prepare.sh:-"
run = ["bash", "-e", "-o", "pipefail", "-s"]
''')
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "FAKE_RAM": str(fake_ram),
                   "XDG_CONFIG_HOME": str(root), "NO_COLOR": "1"}
            result = subprocess.run([executable, "--config", str(profile), "-e", "VALUE=override", "--",
                                     "sh", "-ec", 'test "$VALUE" = override; cat'],
                                    env=env, input="main stdin\n", text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "main stdin\n")
            self.assertEqual((work / "data").read_text(), "copied")
            self.assertEqual((work / "data").stat().st_mode & 0o777, 0o640)
            self.assertEqual((work / "done").read_text(), "teardown")
            self.assertFalse((work / "prepare.sh").exists())
