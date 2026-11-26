import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from oncolab import cli, remote
from oncolab.config import ConfigError, credentials, plan


class Configuration(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(text)
            return plan(config)

    def test_no_implicit_project_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(plan(None)[0], {"mounts": {}, "setup": {}, "entrypoint": []})

    def test_mounts_setup_order_and_entrypoint(self):
        config, origins, host = self.parse("""
entrypoint = ["env", "HOME=/content/example"]
[mounts.work]
source = "gdrive:work"
target = "/content/work"
[mounts.ref]
source = "ref:files"
target = "/mnt/ref"
read_only = true
[setup.last]
run = "true"
depends = ["first", "mounts.work"]
[setup.first]
run = ["printf", "%s", "a b"]
""")
        self.assertFalse(config["mounts"]["mounts.work"]["read_only"])
        self.assertTrue(config["mounts"]["mounts.ref"]["read_only"])
        self.assertEqual(list(config["setup"]), ["first", "last"])
        self.assertEqual(config["setup"]["first"]["argv"][-1], "a b")
        self.assertEqual(config["entrypoint"], ["env", "HOME=/content/example"])
        self.assertEqual(set(origins), {"gdrive", "ref"})
        self.assertIsNone(host)

    def test_bad_dependency_and_cycle(self):
        for text in (
            '[setup.a]\nrun = "true"\ndepends = ["missing"]',
            '[setup.a]\nrun = "true"\ndepends = ["b"]\n[setup.b]\nrun = "true"\ndepends = ["a"]',
        ):
            with self.assertRaises(ConfigError):
                self.parse(text)

    def test_protected_and_overlapping_targets(self):
        for path in ("/", "/bin", "/content", "/content/work/sub", "/content/work/../other"):
            with self.assertRaises(ConfigError, msg=path):
                self.parse(f"""
[mounts.work]
source = "gdrive:work"
target = "/content/work"
[mounts.bad]
source = "gdrive:files"
target = "{path}"
""")

    def test_duplicate_mount_env(self):
        with self.assertRaisesRegex(ConfigError, "environment variable"):
            self.parse("""
[mounts.some-files]
source = "gdrive:a"
target = "/mnt/a"
[mounts.some_files]
source = "gdrive:b"
target = "/mnt/b"
""")

    def test_only_selected_restricted_oauth_sections(self):
        with patch("oncolab.config.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"[demo]\ntype=drive\nscope=drive.file\ntoken=synthetic\n")
            self.assertIn(b"token = synthetic", credentials({"demo": (None, False)}))
            run.return_value = subprocess.CompletedProcess([], 0, b"[demo]\ntype=drive\nscope=drive\n")
            with self.assertRaisesRegex(ConfigError, "restricted"):
                credentials({"demo": (None, False)})


class RemoteMounts(unittest.IsolatedAsyncioTestCase):
    async def test_mount_modes_and_sealing_before_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = remote.DriveMounts(tmp)
            drive.rc = AsyncMock()
            drive.start = AsyncMock()
            drive.config.touch()
            drive.socket.touch()
            mounts = {f"mounts.{name}": {"target": str(Path(tmp).resolve() / name), "source": "demo:"+name,
                                         "read_only": read_only} for name, read_only in (("work", False), ("ref", True))}
            async def run(label, cmd):
                if callable(cmd):
                    await cmd()
                return None
            self.assertEqual(await remote.setup_all(run, {"mounts": mounts, "setup": {}}, drive), 0)
            sent = {call.kwargs["fs"]: call.kwargs["vfsOpt"]["ReadOnly"] for call in drive.rc.await_args_list}
            self.assertEqual(sent, {"demo:work": False, "demo:ref": True})
            self.assertFalse(drive.socket.exists())
            self.assertFalse(drive.config.exists())

    async def test_parallel_setup_and_dependencies(self):
        started = asyncio.Event()
        both = asyncio.Event()
        finishes = []
        async def run(label, _):
            if label == "left":
                started.set()
                await both.wait()
            elif label == "right":
                await started.wait()
                both.set()
            else:
                self.assertCountEqual(finishes, ["left", "right"])
            finishes.append(label)
            return subprocess.CompletedProcess([], 0)
        setup = {name: {"description": name, "argv": ["true"], "depends": deps}
                 for name, deps in (("left", []), ("right", []), ("last", ["left", "right"]))}
        self.assertEqual(await asyncio.wait_for(remote.setup_all(run, {"setup": setup}, None), 1), 0)
        self.assertEqual(finishes[-1], "last")

    async def test_unmount_failure_does_not_kill_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = remote.DriveMounts(tmp)
            drive.server = Mock()
            drive.attached = [Path("/mnt/a"), Path("/mnt/b")]
            with patch("oncolab.remote.sp.call", side_effect=[1, 0]) as call:
                self.assertFalse(drive.close())
                self.assertEqual(call.call_count, 2)
            drive.server.terminate.assert_not_called()


class CLI(unittest.TestCase):
    def test_no_forced_stop_after_ambiguous_disconnect(self):
        with patch.object(cli, "plan", return_value=({}, {}, None)), patch.object(cli, "release") as release, \
                patch.object(cli, "allocate", return_value=("test-session", 0)), \
                patch("sys.argv", ["oncolab", "--", "test", "spaces and $metacharacters"]):
            def session(name, config, ini, hosts, command, forward, terminal, color, attempted):
                self.assertEqual(command, ["test", "spaces and $metacharacters"])
                attempted.set()
                return 255, False
            with patch.object(cli, "run_session", side_effect=session):
                self.assertEqual(cli.main(), 255)
            release.assert_not_called()

    def test_definite_pre_payload_failure_releases(self):
        with patch.object(cli, "plan", return_value=({}, {}, None)), patch.object(cli, "release") as release, \
                patch.object(cli, "allocate", return_value=("test-session", 0)), \
                patch.object(cli, "run_session", return_value=(255, True)), patch("sys.argv", ["oncolab", "test"]):
            self.assertEqual(cli.main(), 255)
            release.assert_called_once()

    def test_entrypoint_inherits_env_and_keeps_exact_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text("""entrypoint = ["sh", "-c", 'export VALUE="hello world"; exec "$@"', "oncolab"]\n""")
            config, _, _ = plan(toml)
        payload = ["sh", "-c", 'printf "%s\\n%s\\n" "$VALUE" "$1"', "payload", 'a b $c']
        result = subprocess.run([*config["entrypoint"], *payload], capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (0, b"hello world\na b $c\n"))
