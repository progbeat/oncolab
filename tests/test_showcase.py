import asyncio
import json
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
    async def test_task_needs_gate_copy_requests_and_stdin_streams_before_producer_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy, stdin = root / "task-0", root / "task-0-input"
            copy.mkdir()
            stdin.mkdir()
            for status in ("ok", "failed", "short"):
                (stdin / f"copy-{status}").write_text(status)
            (root / "broker-pid").write_text(f"{os.getpid()}\n")
            state = root / "state"
            first_bytes, last_bytes = b"task stdin\x00\n", b"rest"
            result, input_result = copy / "copy-result", stdin / "copy-result"
            first_seen = root / "first_seen"
            command = [sys.executable, "-c", (
                "import os,sys; from pathlib import Path;"
                "first=sys.stdin.buffer.readline();"
                "assert not Path(sys.argv[1]).exists();"
                "Path(sys.argv[2]).write_bytes(first);"
                "Path(sys.argv[3]).write_bytes(first+sys.stdin.buffer.read());"
                "assert os.environ['SHOWCASE'] == 'shared'"
            ), str(stdin / "stdin"), str(first_seen), str(root / "out")]
            graph = {
                "first": {"description": "first", "depends": [], "argv": [
                    sys.executable, "-c",
                    "import pathlib,sys,time; time.sleep(.2); pathlib.Path(sys.argv[1]).touch()", str(state),
                ]},
                "second": {"description": "second", "depends": ["first"], "argv": command,
                           "copy_result": str(result), "stdin_result": str(input_result)},
            }
            lifecycle = remote.Lifecycle()
            lifecycle.environment = os.environ | {"SHOWCASE": "shared"}

            async def broker():
                while not result.with_name("requested").exists():
                    await asyncio.sleep(.01)
                self.assertTrue(state.exists(), "copy requested before needs completed")
                self.assertFalse(input_result.with_name("requested").exists())
                result.write_text("ok")
                while not input_result.with_name("requested").exists():
                    await asyncio.sleep(.01)
                peer = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import sys; from oncolab.copy import input_receiver;"
                    "raise SystemExit(input_receiver(sys.argv[1]))", str(input_result),
                    stdin=asyncio.subprocess.PIPE,
                )
                try:
                    peer.stdin.write(len(first_bytes).to_bytes(4, "big") + first_bytes)
                    await peer.stdin.drain()
                    while not first_seen.exists():
                        await asyncio.sleep(.01)
                    self.assertEqual(first_seen.read_bytes(), first_bytes)
                    # The consumer must receive this earlier frame before we send EOF.
                    peer.stdin.write(len(last_bytes).to_bytes(4, "big") + last_bytes + b"\0" * 4)
                    await peer.stdin.drain()
                    peer.stdin.close()
                    self.assertEqual(await peer.wait(), 0)
                finally:
                    if peer.returncode is None:
                        peer.kill()
                        await peer.wait()

            _, status = await asyncio.wait_for(asyncio.gather(
                broker(), lifecycle.prepare(run, {"setup": graph, "jobdir": tmp}, None),
            ), 5)
            self.assertEqual(status, 0)
            self.assertEqual((root / "out").read_bytes(), first_bytes + last_bytes)

    async def test_early_successful_consumer_is_not_a_false_copy_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "-c", "import sys; print(sys.stdin.read(1), end='')"]
            output, sender = await self.run_stream_case(tmp, command, b"x" * (2 * 1024 * 1024), eof=True)
            self.assertEqual(output.returncode, 0)
            self.assertEqual(output.stdout, b"x")
            self.assertEqual(sender, 43)

    async def test_missing_stream_end_marker_fails_even_if_consumer_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
            output, sender = await self.run_stream_case(tmp, command, b"partial", eof=False)
            self.assertEqual(output.returncode, 1)
            self.assertEqual(sender, 42)

    async def run_stream_case(self, tmp, command, body, *, eof):
        root = Path(tmp)
        broker = root / "broker-pid"
        broker.write_text(f"{os.getpid()}\n")
        result = root / "copy-result"
        for status in ("ok", "failed", "short"):
            (root / f"copy-{status}").write_text(status)

        async def peer():
            while not (root / "requested").exists():
                await asyncio.sleep(.01)
            sender = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import sys; from oncolab.copy import input_receiver;"
                "raise SystemExit(input_receiver(sys.argv[1]))", str(result),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                frames = []
                for chunk in (body[i:i+65536] for i in range(0, len(body), 65536)):
                    frames.extend((len(chunk).to_bytes(4, "big"), chunk))
                if eof:
                    frames.append(b"\0" * 4)
                await sender.communicate(b"".join(frames))
                return sender.returncode
            finally:
                if sender.returncode is None:
                    sender.kill()
                    await sender.communicate()

        def launch(stdin, on_start):
            return remote.command_result(command, os.environ, stdin, on_start=on_start)

        return await asyncio.wait_for(asyncio.gather(
            remote.TaskInput(result, broker).run(launch, service=False), peer(),
        ), 5)

    def test_main_reports_service_exit_after_readiness_instead_of_success(self):
        plan = {"setup": {}, "mounts": {}}
        args = ["remote.py", "/unused", "0", json.dumps(plan), "true"]
        with (
            patch.dict(os.environ, {}),
            patch.object(remote.sys, "argv", args),
            patch.object(remote, "enter_workdir"),
            patch.object(remote, "progress", return_value=0, create=True),
            patch.object(remote, "unassign"),
            patch.object(remote.Lifecycle, "check_services", side_effect=RuntimeError("service exited")),
            patch.object(remote.sp, "Popen") as payload,
        ):
            self.assertEqual(remote.main(), 1)
            payload.assert_not_called()

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

    async def test_ready_attempt_can_use_more_than_two_seconds(self):
        lifecycle = remote.Lifecycle()
        lifecycle.environment = os.environ.copy()
        step = {
            "description": "service",
            "start": [sys.executable, "-c", "import time; time.sleep(30)"],
            "ready": [sys.executable, "-c", "import time; time.sleep(2.1)"],
        }
        try:
            await asyncio.wait_for(lifecycle.start("service", step, remote.sp.DEVNULL), 5)
        finally:
            if service := lifecycle.services.get("service"):
                remote.stop_process(service)

    async def test_cancelled_task_input_unblocks_a_waiting_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, broker = root / "copy-result", root / "broker-pid"
            broker.write_text(f"{os.getpid()}\n")
            for status in ("ok", "failed", "short"):
                (root / f"copy-{status}").write_text(status)
            lifecycle = remote.Lifecycle()
            lifecycle.environment = os.environ.copy()
            cleanup = root / "cleaned"
            graph = {"stream": {
                "description": "stream", "depends": [], "stdin_result": str(result),
                "argv": [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                "teardown": ["touch", str(cleanup)],
            }}

            async def peer():
                while not result.with_name("requested").exists():
                    await asyncio.sleep(.01)
                sender = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import sys; from oncolab.copy import input_receiver;"
                    "raise SystemExit(input_receiver(sys.argv[1]))", str(result),
                    stdin=asyncio.subprocess.PIPE,
                )
                try:
                    while not result.with_name("writer-ready").exists():
                        await asyncio.sleep(.01)
                    lifecycle.interrupt(SIGINT, None)
                    # Keep the producer blocked on input until cancellation wakes it.
                    self.assertEqual(await sender.wait(), 43)
                finally:
                    sender.stdin.close()
                    if sender.returncode is None:
                        sender.kill()
                        await sender.wait()

            _, status = await asyncio.wait_for(asyncio.gather(
                peer(), lifecycle.prepare(run, {"setup": graph, "jobdir": tmp}, None),
            ), 5)
            self.assertEqual(status, 130)
            self.assertTrue(lifecycle.cleanup(None))
            self.assertTrue(cleanup.exists())

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
