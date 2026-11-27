import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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
depends = ["first", "work"]
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
            '[setup.a]\nrun = "true"\ndepends = ["missing"]',
            '[setup.a]\nrun = "true"\ndepends = ["b"]\n[setup.b]\nrun = "true"\ndepends = ["a"]',
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

    def test_empty_legacy_mount_table_does_not_select_credentials(self):
        config, path, _ = self.parse('[rclone]\nconfig = "shared.conf"\n[mounts]\n')
        self.assertEqual(config["mounts"], {})
        self.assertIsNone(path)


class Teardown(unittest.TestCase):
    def test_unassign_does_not_import_from_the_project(self):
        with patch.object(remote.sp, "Popen") as launch:
            remote.unassign()
        argv, options = launch.call_args
        self.assertEqual(options["cwd"], "/")
        self.assertEqual(argv[0][1:3], ["-P", "-c"])


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
            sent = {call.kwargs["fs"]: call.kwargs["vfsOpt"]["ReadOnly"] for call in drive.rc.await_args_list if call.args == ("mount/mount",)}
            self.assertEqual(sent, {"demo:work": False, "demo:ref": True})
            self.assertEqual(drive.rc.await_args.args, ("config/setpath",))
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

    def test_profile_copy_and_cli_files_share_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = root / "profile.toml"
            config.write_text('workdir = "/content/job"\n[[copy]]\nlocal = "train:today.py"\n')
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
                    (profile_source, "/content/job/train:today.py"),
                ])
                self.assertNotIn("copies", session.call_args.args[1])
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
