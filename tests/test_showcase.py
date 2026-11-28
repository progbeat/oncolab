import asyncio
import errno
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from signal import SIGINT
from threading import Event, Thread
from unittest.mock import Mock, patch

from oncolab import cli, copy, remote
from oncolab.config import ConfigError, plan


class ShowcaseConfig(unittest.TestCase):
    def test_showcase_normalizes_shared_drive_services_and_copies(self):
        profile = Path(__file__).resolve().parents[1] / "examples" / "showcase.toml"
        config, rclone_path, _ = plan(
            profile, cli_mounts=["source=gdrive:work,target=/content/work"],
            cli_ports=["8080:8765", "9000:90"],
        )
        own = config["setup"]
        self.assertEqual(config["ports"], [(8080, 8765), (9000, 90)])
        self.assertEqual(config["env"], {"COLAB_DRIVE": "/content/work"})
        self.assertEqual(own["work"]["mount"], "mounts.work")
        self.assertEqual(config["mounts"]["mounts.work"]["source"], "gdrive:work")
        self.assertEqual(len(config["mounts"]), 1)
        self.assertEqual(rclone_path, (profile.parent / "rclone-colab.conf").resolve())
        self.assertEqual(own["git"]["copies"], [(str(profile.parent / "install-git.sh"), "-", None)])
        self.assertEqual(own["git-on"]["needs"], ["git"])
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

    def test_explicit_cli_rclone_mount_and_ports_no_implicit_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.write_text('[rclone]\nconfig = "drive.conf"\n')
            for spec, message in (
                ("type=gdrive,source=gdrive:work,target=/content/work", "unknown key"),
                ("source=work,target=/content/work", "REMOTE:PATH"),
                ("source=/tmp/local:work,target=/content/work", "REMOTE:PATH"),
                ("source=-first:work,target=/content/work", "REMOTE:PATH"),
                ("source= first:work,target=/content/work", "REMOTE:PATH"),
                ("source=first :work,target=/content/work", "REMOTE:PATH"),
            ):
                with self.assertRaisesRegex(ConfigError, message):
                    plan(profile, cli_mounts=[spec])
            with self.assertRaisesRegex(ConfigError, "Conflicting remote ports"):
                plan(profile, cli_ports=["8080:80", "8080:90"])
            config, _, _ = plan(profile, cli_mounts=["source=Other Remote:path:more,target=work,readonly",
                                                     "source=team+archive@org:Reports,target=/mnt/reports",
                                                     "source=team@org:,target=/mnt/team"])
            self.assertEqual(config["mounts"]["mounts.cli-1"],
                             {"source": "Other Remote:path:more", "target": "/content/work", "read_only": True})
            self.assertEqual(config["mounts"]["mounts.cli-2"]["source"], "team+archive@org:Reports")
            self.assertEqual(config["mounts"]["mounts.cli-3"]["source"], "team@org:")
            with profile.open("a") as config_file:
                config_file.write('[setup.use]\nrun = ["true"]\nneeds = ["mounts.cli-1"]\n')
            with self.assertRaisesRegex(ConfigError, "unknown dependency"):
                plan(profile, cli_mounts=["source=Other:work,target=/content/work"])


class Transfers(unittest.TestCase):
    def test_cancel_waits_for_cross_filesystem_publication_before_teardown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, destination = root / "source", root / "destination"
            source.write_bytes(b"complete")
            result = root / "copy-result"
            archive = io.BytesIO()
            copy.send_archive(archive, [source])
            archive.seek(0)
            lifecycle = remote.Lifecycle()
            lifecycle.nodes["archive"] = ({
                "description": "Archive", "copy_result": result,
                "teardown": ["unused"],
            }, ())
            lifecycle.began.add("archive")
            publishing, cancelling, resume = Event(), Event(), Event()
            real_copy, real_flock = copy.shutil.copyfileobj, remote.fcntl.flock
            errors, outcomes = [], []

            def transfer(reader, writer, *args):
                if isinstance(writer.name, int):
                    publishing.set()
                    self.assertTrue(resume.wait(3))
                return real_copy(reader, writer, *args)

            def receiver():
                try:
                    copy.receive_archive(archive, [destination], cancelled=root / "cancelled")
                except BaseException as exc:
                    errors.append(exc)

            def flock(fd, flags):
                try:
                    return real_flock(fd, flags)
                except BlockingIOError:
                    cancelling.set()
                    raise

            def hook(*args, **kwargs):
                self.assertEqual(destination.read_bytes(), b"complete")
                destination.unlink()
                return 0

            with (patch.object(copy.os, "link", side_effect=OSError(errno.EXDEV, "cross-device")),
                  patch.object(copy.shutil, "copyfileobj", side_effect=transfer),
                  patch.object(remote.fcntl, "flock", side_effect=flock),
                  patch.object(remote.sp, "call", side_effect=hook) as teardown):
                writer = Thread(target=receiver, daemon=True)
                cleanup = Thread(target=lambda: outcomes.append(lifecycle.cleanup(None)), daemon=True)
                writer.start()
                try:
                    self.assertTrue(publishing.wait(2))
                    cleanup.start()
                    self.assertTrue(cancelling.wait(2))
                    teardown.assert_not_called()
                finally:
                    resume.set()
                    writer.join(3)
                    if cleanup.ident is not None:
                        cleanup.join(3)
            self.assertEqual(errors, [])
            self.assertEqual(outcomes, [True])
            self.assertFalse(destination.exists())

    def test_unconfirmed_archive_quiescence_pins_its_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = remote.Lifecycle()
            lifecycle.nodes.update({
                "mount": ({"description": "Mount", "teardown": ["unmount"]}, ()),
                "archive": ({"description": "Archive", "copy_result": Path(tmp) / "copy-result",
                             "teardown": ["cleanup-archive"]}, ("mount",)),
                "independent": ({"description": "Independent", "teardown": ["cleanup-other"]}, ()),
            })
            lifecycle.began.update(lifecycle.nodes)
            with (patch.object(remote, "quiesce_archive", return_value=False),
                  patch.object(remote.sp, "call", return_value=0) as hook,
                  patch("sys.stderr")):
                self.assertFalse(lifecycle.cleanup(None))
            self.assertTrue((Path(tmp) / "cancelled").exists())
            self.assertEqual([call.args[0] for call in hook.call_args_list], [["cleanup-other"]])

    def test_five_blocked_input_streams_all_start_without_starving_an_archive(self):
        groups = {f"stream-{i}": ((), Path(f"source-{i}")) for i in range(5)}
        groups["archive"] = ([(Path("data.txt"), "/content/data.txt", None)], None)
        archived = Event()
        blockers = [Event() for _ in range(5)]

        class Broker:
            stdout = iter(tuple(f"{name}\n" for name in groups))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def wait(self):
                return 0

        def archive(*_, debug=False):
            if not all(started.wait(1) for started in blockers):
                return "input streams did not start", True
            archived.set()
            return None, True

        def stream(_endpoint, _key, source):
            blockers[int(source.name[-1])].set()
            return (None if archived.wait(2) else "archive starved by stdin streams"), True

        with (
            patch.object(cli.sp, "Popen", return_value=Broker()),
            patch.object(cli, "upload", archive),
            patch.object(cli, "upload_stream", stream),
        ):
            self.assertEqual(cli.upload_tasks([], "the-peer", "/job", groups, "/forwarded"), [])

    def test_late_stream_failure_is_reported_before_the_broker_exits(self):
        reported = Event()

        def lines():
            yield "stream\n"
            if not reported.wait(2):
                raise AssertionError("stream failure must be printed while SSH can still be active")

        class Broker:
            stdout = lines()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def wait(self):
                return 0

        def printing(message, **_):
            self.assertEqual(message, "oncolab: test stream failure")
            reported.set()

        with (patch.object(cli.sp, "Popen", return_value=Broker()),
              patch.object(cli, "upload_stream", return_value=("test stream failure", True)),
              patch("oncolab.cli.print", side_effect=printing, create=True) as output):
            self.assertEqual(cli.upload_tasks([], "peer", "/job", {"stream": ((), Path("input"))},
                                              "/forwarded"), ["test stream failure"])
        output.assert_called_once()

    def test_streamlocal_upload_framing_with_socketpair(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "stdin"
            payload = b"input\0\n" * 10000
            source.write_bytes(payload)
            local, remote_sock = socket.socketpair()
            local.settimeout(5)
            remote_sock.settimeout(5)
            received = []

            class Endpoint:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    local.close()

                def connect(self, destination):
                    self.destination = destination

                def __getattr__(self, name):
                    return getattr(local, name)

            def server():
                with remote_sock, remote_sock.makefile("rb") as stream:
                    received.append(stream.readline())
                    while size := int.from_bytes(stream.read(4), "big"):
                        received.append(stream.read(size))
                    remote_sock.sendall(b"\0")

            worker = Thread(target=server)
            worker.start()
            try:
                with patch.object(cli.socket, "socket", return_value=Endpoint()):
                    self.assertEqual(cli.upload_stream("/forwarded", "task-4-input", source), (None, True))
            finally:
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(received[0], b"task-4-input\n")
            self.assertEqual(b"".join(received[1:]), payload)


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
                "seen=Path(sys.argv[2]); pending=seen.with_suffix('.tmp');"
                "pending.write_bytes(first); pending.replace(seen);"
                "Path(sys.argv[3]).write_bytes(first+sys.stdin.buffer.read());"
                "assert os.environ['SHOWCASE'] == 'shared'"
            ), str(stdin / "stdin"), str(first_seen), str(root / "out")]
            graph = {
                "first": {"description": "first", "needs": [], "argv": [
                    sys.executable, "-c",
                    "import pathlib,sys,time; time.sleep(.2); pathlib.Path(sys.argv[1]).touch()", str(state),
                ]},
                "second": {"description": "second", "needs": ["first"], "argv": command,
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
            "service": {"description": "service", "needs": [],
                        "start": [sys.executable, "-c", "raise SystemExit(4)"],
                        "ready": ["true"]},
            "after": {"description": "after", "needs": ["service"], "argv": ["true"]},
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
                "description": "stream", "needs": [], "stdin_result": str(result),
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
                "service": {"description": "service", "needs": [],
                            "start": [sys.executable, "-c", "import time; time.sleep(30)"]},
                "middle": {"description": "middle", "needs": ["service"], "argv": ["true"],
                           "teardown": ["touch", str(root / "middle-cleaned")]},
                "partial": {"description": "partial", "needs": ["middle"],
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

    async def test_admitted_copy_times_out_start_but_not_an_active_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker, result = root / "broker-pid", root / "copy-result"
            pid = f"{os.getpid()}\n"
            broker.write_text(pid)
            (root / "broker-heartbeat").touch()
            result.with_name("admitted").touch()
            expired = await asyncio.wait_for(remote.wait_copy(result, broker=broker, start_timeout=.02), 1)
            self.assertEqual(expired.returncode, 1)

            async def active_transfer():
                result.with_name("copy-pid").write_text(pid)
                await asyncio.sleep(.25)
                result.write_text("ok")

            _, finished = await asyncio.wait_for(asyncio.gather(active_transfer(),
                remote.wait_copy(result, broker=broker, start_timeout=.02)), 1)
            self.assertEqual(finished.returncode, 0)

    async def test_live_broker_leaves_third_archive_queued_past_receiver_start_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = [root / f"task-{i}" for i in range(3)]
            for directory in dirs:
                directory.mkdir()
                (directory / "requested").touch()
            broker = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import json,sys; from oncolab.copy import transfer_broker;"
                "transfer_broker(sys.argv[1], json.loads(sys.argv[2]))", str(root),
                json.dumps({d.name: False for d in dirs}), stdout=asyncio.subprocess.DEVNULL,
            )

            async def admitted(directory):
                while not (directory / "admitted").exists():
                    await asyncio.sleep(.005)

            waiter = None
            try:
                await asyncio.wait_for(asyncio.gather(*(admitted(d) for d in dirs[:2])), 3)
                last = dirs[2]
                result = last / "copy-result"
                waiter = asyncio.create_task(remote.wait_copy(result, broker=root / "broker-pid", start_timeout=.02))
                await asyncio.sleep(.25)
                self.assertFalse((last / "admitted").exists())
                self.assertFalse(waiter.done(), "queued copy expired before an archive slot was free")
                (dirs[0] / "copy-result").write_text("ok")
                await asyncio.wait_for(admitted(last), 3)
                (last / "copy-pid").write_text(f"{os.getpid()}\n")
                await asyncio.sleep(.25)
                self.assertFalse(waiter.done(), "active copy incorrectly hit the startup deadline")
                result.write_text("ok")
                self.assertEqual((await asyncio.wait_for(waiter, 1)).returncode, 0)
                (dirs[1] / "copy-result").write_text("ok")
                self.assertEqual(await asyncio.wait_for(broker.wait(), 3), 0)
            finally:
                if waiter is not None and not waiter.done():
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                if broker.returncode is None:
                    broker.kill()
                    await broker.wait()

    async def test_ready_service_can_read_stdin_only_after_the_main_command_triggers_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready, trigger, received = (root / name for name in ("ready", "trigger", "received"))
            result, broker = root / "copy-result", root / "broker-pid"
            broker.write_text(f"{os.getpid()}\n")
            for status in ("ok", "failed", "short"):
                result.with_name(f"copy-{status}").write_text(status)
            server = """
import sys, time
from pathlib import Path
ready, trigger, received = map(Path, sys.argv[1:])
ready.touch()
while not trigger.exists():
    time.sleep(.01)
received.write_bytes(sys.stdin.buffer.read())
time.sleep(30)
"""
            graph = {"listener": {"description": "listener", "needs": [],
                                  "start": [sys.executable, "-c", server, *map(str, (ready, trigger, received))],
                                  "ready": ["test", "-f", str(ready)], "stdin_result": str(result)}}
            lifecycle = remote.Lifecycle()
            lifecycle.environment = os.environ.copy()

            async def open_sender():
                while not result.with_name("requested").exists():
                    await asyncio.sleep(.01)
                return await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import sys; from oncolab.copy import input_receiver;"
                    "raise SystemExit(input_receiver(sys.argv[1]))", str(result),
                    stdin=asyncio.subprocess.PIPE,
                )

            connecting = asyncio.create_task(open_sender())
            sender = None
            try:
                status = await asyncio.wait_for(lifecycle.prepare(run, {"setup": graph, "jobdir": tmp}, None), 5)
                self.assertEqual(status, 0)
                sender = await asyncio.wait_for(connecting, 5)
                self.assertFalse(result.exists(), "startup waited for the input source to finish")
                self.assertFalse(result.with_name("stdin").exists())
                self.assertFalse(result.with_name("cancelled").exists())
                # The ready service has not read from stdin yet. Only the main
                # command's trigger allows it to drain and then see EOF.
                trigger.touch()
                await asyncio.wait_for(sender.communicate(b"\0\0\0\x05hello" + b"\0" * 4), 5)
                self.assertEqual(sender.returncode, 0)

                async def wait_for_read():
                    while not received.exists() or received.stat().st_size < 5:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait_for_read(), 5)
                self.assertEqual(received.read_bytes(), b"hello")
            finally:
                if connecting.done() and not connecting.cancelled():
                    sender = connecting.result()
                else:
                    connecting.cancel()
                    await asyncio.gather(connecting, return_exceptions=True)
                if sender is not None and sender.returncode is None:
                    sender.kill()
                    await sender.communicate()
                self.assertTrue(lifecycle.cleanup(None))

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
