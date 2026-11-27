import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from signal import SIGINT
from unittest.mock import Mock, patch

from oncolab import cli, remote
from oncolab.config import ConfigError, plan


class ShowcaseConfig(unittest.TestCase):
    def test_showcase_normalizes_shared_drive_services_and_copies(self):
        profile = Path(__file__).resolve().parents[1] / "examples" / "showcase.toml"
        config, origins, _ = plan(
            profile, cli_mounts=["type=gdrive,source=work,target=/content/work"],
            cli_ports=["8080:8765", "9000:90"],
        )
        own = config["setup"]
        self.assertEqual(config["ports"], [(8080, 8765), (9000, 90)])
        self.assertEqual(config["env"], {"COLAB_DRIVE": "/content/work"})
        self.assertEqual(own["work"]["mount"], "mounts.work")
        self.assertEqual(config["mounts"]["mounts.work"]["source"], "gdrive:work")
        self.assertEqual(len(config["mounts"]), 1)
        self.assertEqual(origins, {"gdrive": (profile.parent / "rclone-colab.conf", False)})
        self.assertEqual(own["git"]["copies"], [(str(profile.parent / "install-git.sh"), "-", None)])
        self.assertEqual(own["git-on"]["depends"], ["git"])
        self.assertEqual(own["preview"]["start"][:3], ["python3", "-m", "http.server"])
        self.assertIn("teardown", own["preview"])

    def test_copy_modes_exact_argv_and_rejected_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stdin.txt").write_text("hello")
            (root / "regular.txt").write_text("there")
            profile = root / "config.toml"
            profile.write_text("""
[setup.a]
copy = ["stdin.txt:-", {source = "regular.txt", target = "relative/plain", chmod = 0o000}]
run = ["echo", ""]
""")
            config, _, _ = plan(profile)
            self.assertEqual(config["setup"]["a"]["argv"], ["echo", ""])
            top, grouped = cli.prepare_copies(config, [], [])
            self.assertFalse(top)
            self.assertEqual(grouped["a"], [
                ((root / "stdin.txt").resolve(), "-", 0o600),
                ((root / "regular.txt").resolve(), "/content/relative/plain", 0),
            ])
            for contents in (
                '[setup.a]\ncopy = ["a:-", "b:-"]\nrun = "true"',
                '[setup.a]\ncopy = "a:-"',
                '[setup.a]\nstart = [""]',
                '[setup.a]\nrun = "true"\nstart = "true"',
                '[setup.a]\nrun = "true"\nready = "true"',
                '[setup.a]\nrun = "true"\nneeds = ["b"]\ndepends = []',
                'ports = ["0:80"]',
            ):
                with self.subTest(contents=contents):
                    profile.write_text(contents)
                    with self.assertRaises(ConfigError):
                        plan(profile)

    def test_explicit_cli_drive_mount_and_ports_no_implicit_bind(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            bad = ["type=bind,source=work,target=/content/work",
                   "source=work,target=/content/work"]
            for spec in bad:
                with self.assertRaisesRegex(ConfigError, "type=gdrive"):
                    plan(None, cli_mounts=[spec])
            with self.assertRaisesRegex(ConfigError, "Conflicting remote ports"):
                plan(None, cli_ports=["8080:80", "8080:90"])
            config, _, _ = plan(None, cli_mounts=["type=gdrive,source=work,target=work,readonly"])
            self.assertEqual(config["mounts"]["mounts.cli-1"],
                             {"source": "gdrive:work", "target": "/content/work", "read_only": True})


async def run(_, command):
    if callable(command):
        return await command()


class ShowcaseRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_task_needs_gate_copy_requests_and_stdin_is_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "task-0"
            case.mkdir()
            (root / "broker-pid").write_text(f"{os.getpid()}\n")
            state = root / "state"
            payload = b"task stdin\x00\n"
            result = case / "copy-result"
            data = case / "stdin"
            command = [sys.executable, "-c", (
                "import os,sys; from pathlib import Path;"
                "assert not Path(sys.argv[1]).exists();"
                "Path(sys.argv[2]).write_bytes(sys.stdin.buffer.read());"
                "assert os.environ['SHOWCASE'] == 'shared'"
            ), str(data), str(root / "out")]
            graph = {
                "first": {"description": "first", "depends": [], "argv": [
                    sys.executable, "-c",
                    "import pathlib,sys,time; time.sleep(.2); pathlib.Path(sys.argv[1]).touch()", str(state),
                ]},
                "second": {"description": "second", "depends": ["first"], "argv": command,
                           "copy_result": str(result), "stdin": str(data)},
            }
            lifecycle = remote.Lifecycle()
            lifecycle.environment = os.environ | {"SHOWCASE": "shared"}

            async def broker():
                while not result.with_name("requested").exists():
                    await asyncio.sleep(.01)
                self.assertTrue(state.exists(), "copy requested before needs completed")
                data.write_bytes(payload)
                result.write_text("ok")

            _, status = await asyncio.wait_for(asyncio.gather(
                broker(), lifecycle.prepare(run, {"setup": graph, "jobdir": tmp}, None),
            ), 5)
            self.assertEqual(status, 0)
            self.assertEqual((root / "out").read_bytes(), payload)

    async def test_early_service_exit_cannot_pass_ready_or_start_dependent_task(self):
        lifecycle = remote.Lifecycle()
        lifecycle.environment = os.environ.copy()
        graph = {
            "service": {"description": "service", "depends": [],
                        "start": [sys.executable, "-c", "raise SystemExit(4)"],
                        "ready": ["true"]},
            "after": {"description": "after", "depends": ["service"], "argv": ["true"]},
        }
        try:
            with self.assertRaisesRegex(RuntimeError, "exited before readiness"):
                await lifecycle.prepare(run, {"setup": graph}, None)
            self.assertNotIn("setup:after", lifecycle.began)
        finally:
            self.assertTrue(lifecycle.cleanup(None))

    async def test_cancelled_partial_task_runs_teardown_and_keeps_transitive_resources_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entered = root / "entered"
            lifecycle = remote.Lifecycle()
            lifecycle.environment = os.environ.copy()
            graph = {
                "service": {"description": "service", "depends": [],
                            "start": [sys.executable, "-c", "import time; time.sleep(30)"]},
                "middle": {"description": "middle", "depends": ["service"], "argv": ["true"],
                           "teardown": ["touch", str(root / "middle-cleaned")]},
                "partial": {"description": "partial", "depends": ["middle"],
                            "argv": [sys.executable, "-c", (
                                "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
                            ), str(entered)],
                            "teardown": ["bash", "-c", 'touch "$1"; exit 6', "--", str(root / "partial-cleaned")]},
            }

            async def interrupt():
                while not entered.exists():
                    await asyncio.sleep(.01)
                lifecycle.interrupt(SIGINT, None)

            try:
                _, code = await asyncio.wait_for(asyncio.gather(
                    interrupt(), lifecycle.prepare(run, {"setup": graph}, None),
                ), 5)
                self.assertEqual(code, 130)
                self.assertFalse(lifecycle.cleanup(None))
                self.assertTrue((root / "partial-cleaned").exists())
                self.assertFalse((root / "middle-cleaned").exists())
                self.assertIsNone(lifecycle.services["setup:service"].poll())
            finally:
                if service := lifecycle.services.get("setup:service"):
                    remote.stop_process(service)

    async def test_broker_death_fails_promptly_even_while_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "broker-pid").write_text("456789\n")
            with patch.object(remote.os, "kill", side_effect=ProcessLookupError):
                result = await asyncio.wait_for(remote.wait_copy(path / "copy-result",
                    broker=path / "broker-pid"), 1)
            self.assertEqual(result.returncode, 1)

    def test_drive_hook_failure_preserves_mount_and_daemon(self):
        lifecycle = remote.Lifecycle()
        lifecycle.environment = os.environ.copy()
        lifecycle.nodes = {
            "rcd": ({"description": "rclone"}, ()),
            "setup:work": ({"description": "Drive", "mount_target": "/content/work"}, ("rcd",)),
            "setup:render": ({"description": "Render", "teardown": ["false"]}, ("setup:work",)),
        }
        lifecycle.began = set(lifecycle.nodes)
        drive = Mock()
        self.assertFalse(lifecycle.cleanup(drive))
        drive.detach.assert_not_called()
        drive.close.assert_called_once_with({Path("/content/work")}, keep_daemon=True)
