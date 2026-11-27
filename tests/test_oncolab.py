import asyncio
import base64
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock, patch

from oncolab import cli, remote
from oncolab.config import ConfigError, plan


class Configuration(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(text)
            return plan(config)

    def test_no_implicit_project_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(plan(None)[0], {"mounts": {}, "setup": {}, "entrypoint": [], "workdir": "/content", "env": {}})

    def test_mounts_setup_order_and_entrypoint(self):
        config, rclone_path, host = self.parse("""
entrypoint = ["env", "HOME=/content/example"]
[rclone]
config = "drive.conf"
[setup.work]
mount = { source = "gdrive:work", target = "/content/work" }
[setup.ref]
mount = { source = "ref:files", target = "/mnt/ref", readonly = true }
[setup.last]
run = "true"
needs = ["first", "work"]
[setup.first]
run = ["printf", "%s", "a b"]
""")
        self.assertFalse(config["mounts"]["mounts.work"]["read_only"])
        self.assertTrue(config["mounts"]["mounts.ref"]["read_only"])
        self.assertLess(list(config["setup"]).index("work"), list(config["setup"]).index("last"))
        self.assertLess(list(config["setup"]).index("first"), list(config["setup"]).index("last"))
        self.assertEqual(config["setup"]["first"]["argv"][-1], "a b")
        self.assertEqual(config["entrypoint"], ["env", "HOME=/content/example"])
        self.assertEqual(rclone_path.name, "drive.conf")
        self.assertEqual(config["mounts"]["mounts.ref"]["source"], "ref:files")
        self.assertIsNone(host)

    def test_bad_dependency_and_cycle(self):
        for text in (
            '[setup.a]\nrun = "true"\nneeds = ["missing"]',
            '[setup.a]\nrun = "true"\nneeds = ["b"]\n[setup.b]\nrun = "true"\nneeds = ["a"]',
        ):
            with self.assertRaises(ConfigError):
                self.parse(text)

    def test_protected_and_overlapping_targets(self):
        for path in ("/", "/bin", "/content", "/content/work/sub", "/content/work/../other"):
            with self.assertRaises(ConfigError, msg=path):
                self.parse(f"""
[setup.work]
mount = {{ source = "gdrive:work", target = "/content/work" }}
[setup.bad]
mount = {{ source = "gdrive:files", target = "{path}" }}
""")

    def test_duplicate_mount_env(self):
        with self.assertRaisesRegex(ConfigError, "environment variable"):
            self.parse("""
[setup.some-files]
mount = { source = "gdrive:a", target = "/mnt/a" }
[setup.some_files]
mount = { source = "gdrive:b", target = "/mnt/b" }
""")

    def test_legacy_profile_keys_are_rejected(self):
        for contents, key in (
            ('[setup.old]\nscript = "not-read.sh"\n', "script"),
            ('[setup.old]\nrun = ["true"]\ndepends = []\n', "depends"),
            ('[rclone]\nallow_full_drive = true\n', "allow_full_drive"),
            ('[mounts]\n', "Legacy"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                self.parse(contents)


class Teardown(unittest.TestCase):
    def test_unassign_does_not_import_from_the_project(self):
        with patch.object(remote.sp, "Popen") as launch:
            remote.unassign()
        argv, options = launch.call_args
        self.assertEqual(options["cwd"], "/")
        self.assertEqual(argv[0][1:3], ["-P", "-c"])

    def test_early_workdir_failure_scrubs_claimed_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "secret-stage"
            stage.mkdir(mode=0o700)
            pending = stage / "rclone.conf"
            pending.write_bytes(b"unrelated private sections")
            workdir = root / "regular-file"
            workdir.touch()
            plan = {"workdir": str(workdir), "mounts": {
                "mounts.work": {"target": "/mnt/work"}}}
            argv = ["remote.py", str(stage), "0", json.dumps(plan), "unused"]
            real_enter = remote.enter_workdir

            def enter(config):
                self.assertFalse(pending.exists())
                self.assertEqual((stage / "active.conf").read_bytes(), b"unrelated private sections")
                real_enter(config)

            with (patch("sys.argv", argv), patch.dict(os.environ),
                  patch.object(remote, "enter_workdir", side_effect=enter),
                  patch.object(remote, "unassign") as unassign, patch("sys.stderr", new_callable=io.StringIO)):
                self.assertEqual(remote.main(), 1)
            self.assertFalse(stage.exists())
            unassign.assert_called_once_with()


class CommandHelp(unittest.TestCase):
    def test_mount_help_shows_readonly(self):
        with (patch("sys.argv", ["oncolab", "--help"]), patch("sys.stdout", new_callable=io.StringIO) as stdout,
              self.assertRaises(SystemExit) as exit_context):
            cli.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("target=PATH[,readonly]", stdout.getvalue())
        self.assertIn("read-only", stdout.getvalue())


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
            lifecycle = remote.Lifecycle()
            lifecycle.environment = {"HOME": "/profile/home", "PATH": "/profile/bin", "MODE": "shared",
                                     "RCLONE_RC_BASEURL": "/alt", "RCLONE_LOOPBACK": "true",
                                     "RCLONE_URL": "https://example.invalid/", "RCLONE_DRIVE_SCOPE": "drive.readonly"}
            expected = {"HOME": "/profile/home", "PATH": "/profile/home/.local/bin:/profile/bin:/usr/bin:/bin",
                        "MODE": "shared", "RCLONE_DRIVE_SCOPE": "drive.readonly"}
            with (patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
                  patch.object(remote, "command_result", new_callable=AsyncMock) as install):
                install.return_value = subprocess.CompletedProcess([], 0)
                status = await remote.setup_all(run, {"mounts": mounts, "setup": {}}, drive, lifecycle)
            self.assertEqual(status, 0)
            self.assertCountEqual([call.args[0][-1] for call in install.await_args_list],
                                  [remote.FUSE, remote.RCLONE])
            for call in install.await_args_list:
                self.assertIs(call.args[1], drive.environment)
            self.assertEqual(drive.environment, expected)
            self.assertEqual(lifecycle.environment["PATH"], "/profile/bin")
            self.assertEqual(lifecycle.environment["RCLONE_RC_BASEURL"], "/alt")
            sent = {call.kwargs["fs"]: call.kwargs["vfsOpt"]["ReadOnly"] for call in drive.rc.await_args_list if call.args == ("mount/mount",)}
            self.assertEqual(sent, {"demo:work": False, "demo:ref": True})
            self.assertEqual(drive.rc.await_args.args, ("config/setpath",))
            self.assertFalse(drive.socket.exists())
            self.assertFalse(drive.config.exists())

    async def test_rclone_daemon_and_client_use_profile_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = remote.DriveMounts(tmp)
            drive.environment = {"PATH": "/profile/bin:/usr/bin", "MODE": "shared"}
            with (patch.object(remote.sp, "Popen") as daemon, patch.object(Path, "is_socket", return_value=True),
                  patch.object(remote, "command_result", new_callable=AsyncMock) as client):
                client.return_value = subprocess.CompletedProcess([], 0)
                await drive.start()
                await drive.rc("config/setpath", path="/dev/null")
            self.assertIs(daemon.call_args.kwargs["env"], drive.environment)
            self.assertIs(client.await_args.args[1], drive.environment)

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
        setup = {name: {"description": name, "argv": ["true"], "needs": deps}
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
    def test_legacy_mount_profile_never_reads_or_forwards_shared_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile.toml"
            (root / "shared.conf").write_bytes(b"[first]\n[private-unrelated]\n")
            profile.write_text("""
[rclone]
config = "shared.conf"
[mounts.work]
source = "first:work"
target = "/mnt/work"
""")
            with (patch("sys.argv", ["oncolab", "--config", str(profile), "--", "true"]),
                  patch.object(cli, "read_rclone_config") as read_config,
                  patch.object(cli, "allocate") as allocate,
                  patch.object(cli, "run_session") as run_session,
                  patch("sys.stderr", new_callable=io.StringIO) as stderr,
                  self.assertRaises(SystemExit) as exit_status):
                cli.main()
            self.assertEqual(exit_status.exception.code, 2)
            self.assertIn("Legacy [mounts.*] entries must be migrated", stderr.getvalue())
            self.assertIn("transfers the whole file", stderr.getvalue())
            read_config.assert_not_called()
            allocate.assert_not_called()
            run_session.assert_not_called()

    def test_unused_symlink_loop_is_ignored_but_selected_config_fails_before_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile.toml"
            profile.write_text('[rclone]\nconfig = "loop"\n')
            (root / "loop").symlink_to("loop")
            self.assertIsNone(plan(profile)[1])
            argv = ["oncolab", "--config", str(profile), "--mount",
                    "source=first:path,target=/mnt/a", "--", "true"]
            with (patch("sys.argv", argv), patch.object(cli, "allocate") as allocate,
                  patch("sys.stderr", new_callable=io.StringIO) as stderr,
                  self.assertRaises(SystemExit) as exit_status):
                cli.main()
            self.assertEqual(exit_status.exception.code, 2)
            self.assertIn("loop", stderr.getvalue())
            allocate.assert_not_called()

    def test_ambiguous_ssh_recovery_removes_only_pending_before_closing_master(self):
        for cleanup_status in (0, 255):
            with self.subTest(cleanup_status=cleanup_status):
                operations = []

                def run(argv, **kwargs):
                    if kwargs.get("input"):
                        [(stage, files)] = json.loads(kwargs["input"]).items()
                        self.assertEqual(list(files), ["rclone.conf"])
                        operations.append(stage)
                    elif "-O" in argv:
                        operations.append("close")
                    else:
                        self.assertEqual(shlex.split(argv[-1]), ["rm", "-f", "--", operations[0] + "/rclone.conf"])
                        self.assertIn("ProxyCommand=false", argv)
                        self.assertEqual(kwargs["timeout"], 5)
                        operations.append("recover")
                        return subprocess.CompletedProcess(argv, cleanup_status)
                    return subprocess.CompletedProcess(argv, 0)

                with (patch.object(cli, "ssh_options", return_value=(["colab"], [], None)),
                      patch.object(cli.sp, "run", side_effect=run), patch.object(cli.sp, "call", return_value=255),
                      patch("sys.stderr", new_callable=io.StringIO) as stderr):
                    result = cli.run_session("failed-session", {"setup": {}}, b"private", None,
                                             ["true"], False, False, False, Event())
                self.assertEqual(result, (255, False))
                self.assertEqual(operations[1:], ["recover", "close"])
                self.assertEqual("could not confirm cleanup" in stderr.getvalue(), bool(cleanup_status))

    def test_mount_without_existing_explicit_config_fails_before_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile.toml"
            default = root / "rclone"
            default.mkdir()
            (default / "rclone.conf").write_bytes(b"must never read this")
            for text, error in (("", "explicit [rclone].config"),
                                ('[rclone]\nconfig = "missing.conf"\n', "config file not found")):
                with self.subTest(text=text):
                    profile.write_text(text)
                    args = ["oncolab", "--config", str(profile),
                            "--mount", "source=first:path,target=/mnt/a", "--", "true"]
                    with (patch("sys.argv", args), patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
                          patch.object(cli, "allocate") as allocate,
                          patch("sys.stderr", new_callable=io.StringIO) as stderr,
                          self.assertRaises(SystemExit) as exit_status):
                        cli.main()
                    self.assertEqual(exit_status.exception.code, 2)
                    self.assertIn(error, stderr.getvalue())
                    allocate.assert_not_called()

    def test_whole_rclone_config_uses_only_private_bootstrap_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile, config_file = root / "profile.toml", root / "selected.conf"
            raw = (b"[first]\r\ntype=drive\r\ntoken=SECRET_FIXTURE\r\n"
                   b"[second]\r\ntype=crypt\r\n[unused]\r\nopaque=\xff\r\n")
            config_file.write_bytes(raw)
            profile.write_text('[rclone]\nconfig = "selected.conf"\n')
            args = ["oncolab", "--config", str(profile), "--mount", "source=first:two:three,target=/mnt/a",
                    "--mount", "source=second:,target=/mnt/b,readonly", "--", "true"]
            bootstrap = []

            def run(argv, **kwargs):
                self.assertFalse(argv[-1].startswith("rm "))
                self.assertNotIn("SECRET_FIXTURE", repr(argv))
                self.assertNotIn(base64.b64encode(raw).decode("ascii"), repr(argv))
                if staged := kwargs.get("input"):
                    bootstrap.append(staged)
                return subprocess.CompletedProcess(argv, 0)

            with (patch("sys.argv", args), patch.object(cli, "allocate", return_value=("test-session", 0)),
                  patch.object(cli, "ssh_options", return_value=(["colab"], [], None)),
                  patch.object(cli.sp, "run", side_effect=run),
                  patch.object(cli.sp, "call", side_effect=lambda argv: run(argv).returncode),
                  patch("sys.stderr", new_callable=io.StringIO) as stderr):
                self.assertEqual(cli.main(), 0)
            self.assertNotIn("SECRET_FIXTURE", stderr.getvalue())
            [envelope] = bootstrap
            [(directory, files)] = json.loads(envelope).items()
            self.assertTrue(directory.startswith("/dev/shm/oncolab-"))
            target = root / "private"
            subprocess.run([sys.executable, "-c", cli.STAGE],
                           input=json.dumps({str(target): files}).encode(), check=True)
            staged = target / "rclone.conf"
            self.assertEqual(staged.read_bytes(), raw)
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_named_copy_and_cli_copy_reject_overlapping_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "profile.toml"
            config.write_text('workdir = "/content/job"\n[setup.copy]\ncopy = { source = "train:today.py" }\n')
            profile_source = root / "train:today.py"
            cli_source = root / "extra.txt"
            profile_source.touch()
            cli_source.touch()
            command = ["oncolab", "--config", str(config), "--copy", str(cli_source)]
            with patch.object(cli, "allocate", return_value=("session", 0)) as allocate, \
                    patch.object(cli, "run_session", return_value=(0, False)) as session:
                with patch("sys.argv", [*command, "--", "true"]):
                    self.assertEqual(cli.main(), 0)
                self.assertEqual(session.call_args.kwargs["copies"], [
                    (cli_source, "/content/job/extra.txt"),
                ])
                self.assertEqual(session.call_args.kwargs["task_copies"], {
                    "copy": [(profile_source, "/content/job/train:today.py", None)],
                })
                allocate.reset_mock()
                with patch("sys.argv", [*command, "--copy-to", str(cli_source), "train:today.py", "--", "true"]), \
                        patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as result:
                        cli.main()
                    self.assertEqual(result.exception.code, 2)
                    self.assertIn("Overlapping --copy destination", stderr.getvalue())
                allocate.assert_not_called()

    def test_no_forced_stop_after_ambiguous_disconnect(self):
        with patch.object(cli, "plan", return_value=({"env": {}, "workdir": "/content", "mounts": {}}, None, None)), patch.object(cli, "release") as release, \
                patch.object(cli, "allocate", return_value=("test-session", 0)), \
                patch("sys.argv", ["oncolab", "--", "test", "spaces and $metacharacters"]):
            def session(name, config, ini, hosts, command, forward, terminal, color, attempted, *, copies):
                self.assertEqual(command, ["test", "spaces and $metacharacters"])
                attempted.set()
                return 255, False
            with patch.object(cli, "run_session", side_effect=session):
                self.assertEqual(cli.main(), 255)
            release.assert_not_called()

    def test_definite_pre_payload_failure_releases(self):
        with patch.object(cli, "plan", return_value=({"env": {}, "workdir": "/content", "mounts": {}}, None, None)), patch.object(cli, "release") as release, \
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

    def test_recognizes_wrapped_colab_service_unavailable(self):
        self.assertTrue(cli.allocation_unavailable(
            b"Trail\nColabRequestError: Failed to issue request POST \r\n"
            b"https://colab.research.google.com/tun/m/assign?nbh=ab\r\nc&variant=DEFAULT:"
            b"\r\n Service Unavailable\r\n"))
        self.assertFalse(cli.allocation_unavailable(b"ColabRequestError: Unrelated Service Unavailable"))
