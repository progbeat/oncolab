import errno
import inspect
import io
import json
import os
import pty
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from oncolab import ssh_pipeline
from oncolab.cli import call_command_ssh, embedded_gate
from oncolab.duration import format_duration
from oncolab.progress import SetupTimeline, completed_steps, display_label, progress, setup_summary_socket
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
    result = await real(argv, env, *args, **kwargs)
    if 'Path("independent").touch()' in argv[-1]:
        # The script's own marker appears before its process exits.
        (root / "independent-finished").touch()
    return result

accept = remote.SetupGate.accept
async def tracked_accept(self):
    await accept(self)
    (root / "control-attached").touch()

remote.SetupGate.accept = tracked_accept
remote.DriveMounts = FakeDrive
remote.command_result = run
def unassign():
    if (root / "fail-release").exists():
        raise OSError("unassign unavailable")
    (root / "unassigned").touch()

remote.unassign = unassign
raise SystemExit(remote.main())
'''


def wait_file(path):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    if not path.exists():
        raise AssertionError(f"{path.name} did not appear")


class ParallelSSH(unittest.TestCase):
    def test_command_trampoline_restores_default_sigpipe(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "wb") as admission:
            admission.write(packet("/", {"PATH": os.defpath}, False))
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", ssh_pipeline.COMMAND_ENTRY, str(read_fd),
                 "/bin/sh", "-c", "kill -PIPE $$; echo survived"],
                pass_fds=(read_fd,), capture_output=True, timeout=5,
            )
        finally:
            os.close(read_fd)
        self.assertEqual((result.returncode, result.stdout), (-signal.SIGPIPE, b""))

    def test_owner_sends_only_one_cancel_when_start_is_in_flight(self):
        gate = Mock()
        child = Mock()
        with (patch.object(ssh_pipeline, "receive", side_effect=[["started", [123, True]], ["exit", 130]]),
              patch.object(ssh_pipeline, "CommandHandle", return_value=child)):
            self.assertEqual(ssh_pipeline.SetupGate.receive_status(gate, lambda: signal.SIGINT), 130)
        gate.send.assert_called_once_with("cancel")
        child.close.assert_called_once()

    def test_gate_loss_after_pty_admission_does_not_confirm_cleanup(self):
        gate = Mock()
        child = Mock()
        with (patch.object(ssh_pipeline, "receive",
                           side_effect=[["started", [123, True]], ConnectionError("gate disappeared")]),
              patch.object(ssh_pipeline, "CommandHandle", return_value=child),
              self.assertRaises(ssh_pipeline.CommandStateUnknown)):
            ssh_pipeline.SetupGate.receive_status(gate, lambda: False)
        gate.send.assert_called_once_with("go")
        child.stop.assert_called_once()
        child.close.assert_called_once()

    def test_command_channel_retries_only_a_confirmed_pre_dispatch_refusal(self):
        refused = b"mux_client_request_session: session request failed: Session open refused by peer\n"
        lost = b"mux_client_request_session: read from master failed: Broken pipe\n"
        exit_255 = b"debug2: Received exit status from master 255\n"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ssh.log"
            owner = Mock()
            owner.poll.return_value = None
            for cases, expected, waits in (
                ([(255, refused), (255, refused), (0, None)], (0, b"", False), [1, 2]),
                ([(255, refused)] * 3, (255, refused, True), [1, 2]),
                ([(255, refused), (255, None)], (255, b"", False), [1]),
                ([(255, lost)], (255, lost, False), []),
                ([(255, exit_255)], (255, exit_255, False), []),
            ):
                with self.subTest(cases=cases):
                    reports = iter(cases)

                    def run(argv):
                        self.assertFalse(log.exists())
                        status, diagnostic = next(reports)
                        if diagnostic is not None:
                            log.write_bytes(diagnostic)
                        return status

                    with patch("oncolab.cli.sp.call", side_effect=run) as ssh, patch("oncolab.cli.sleep") as sleep:
                        self.assertEqual(call_command_ssh(["ssh"], log, None, owner), expected)
                        self.assertEqual(ssh.call_count, len(cases))
                        self.assertEqual([args[0] for args, _ in sleep.call_args_list], waits)

    def test_gate_failure_erases_the_handoff_before_diagnostic(self):
        for took_over in (False, True):
            with self.subTest(took_over=took_over):
                terminal = io.StringIO()

                def fail(*args):
                    if took_over:
                        args[-1](erase=False)
                    raise OSError("connection closed")

                def diagnostic(data):
                    terminal.write(data.decode())

                with (patch.object(sys, "argv", ["gate", "/socket", "0", '{"terminal_handoff":"waiting"}', "cmd"]),
                      patch.object(sys, "stdout", terminal),
                      patch.object(ssh_pipeline, "command_gate", side_effect=fail),
                      patch.object(ssh_pipeline, "pipeline_stderr", side_effect=diagnostic)):
                    self.assertEqual(ssh_pipeline.command_gate_main(None), 1)
                prefix = "\x1b[A\r" if took_over else "\x1b[A\r\x1b[K"
                self.assertTrue(terminal.getvalue().startswith(prefix + "[Setup channel closed]"))
                self.assertEqual(terminal.getvalue().count("\x1b[A"), 1)

    def test_native_pty_delivers_ctrl_c_to_admitted_foreground_group(self):
        owner, gate = socket.socketpair()
        pid, fd = pty.fork()
        if pid == 0:
            owner.close()
            gate_fd = gate.fileno()
            os.set_inheritable(gate_fd, True)
            command = (
                "import os, socket, sys; from oncolab.ssh_pipeline import gated_command; "
                "gate = socket.socket(fileno=int(sys.argv[1])); "
                "print('EXIT', gated_command(gate, bytearray(), '/', dict(os.environ), "
                "[sys.executable, '-c', "
                "'import os,time; assert os.tcgetpgrp(0) == os.getpgrp(); "
                "print(\"command ready\",flush=True); time.sleep(60)'])); "
            )
            os.execv(sys.executable, [sys.executable, "-c", command, str(gate_fd)])
        gate.close()
        output = bytearray()
        try:
            owner.settimeout(6)
            owner_input = owner.makefile("rb")
            self.assertEqual(json.loads(owner_input.readline())[0], "started")
            owner.sendall(packet("go"))
            sent = False
            deadline = time.monotonic() + 8
            while b"EXIT 130" not in output and time.monotonic() < deadline:
                if not select.select([fd], [], [], .1)[0]:
                    continue
                output.extend(os.read(fd, 65536))
                if not sent and b"command ready" in output:
                    os.write(fd, b"\x03")
                    sent = True
            self.assertIn(b"EXIT 130", output)
        finally:
            owner.close()
            os.close(fd)
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    def test_setup_precedes_drive_config_and_command_waits(self):
        self.run_pipeline()

    def test_timings_retains_only_the_blocking_jobs_on_stderr(self):
        self.run_pipeline(timings=True)

    def test_command_disconnect_cancels_setup_and_releases_vm(self):
        self.run_pipeline(disconnect=True)

    def test_sigint_during_setup_cancels_both_channels_with_status_130(self):
        self.run_pipeline(cancel_client=True)

    def test_failed_setup_skips_command_and_unassigns(self):
        self.run_pipeline(setup_fails=True)

    def test_sigterm_to_setup_owner_stops_running_piped_command(self):
        self.run_pipeline(cancel_owner=True)

    def test_vm_release_start_failure_is_reported_to_command_client(self):
        self.run_pipeline(release_fails=True)

    def test_user_environment_is_applied_after_admission(self):
        self.run_pipeline(native_env=True)

    def test_payload_exit_125_does_not_report_vm_release_failure(self):
        self.run_pipeline(payload_status=125)

    def test_teardown_output_survives_backpressure_before_vm_release(self):
        self.run_pipeline(slow_stderr=True, payload_status=0)

    def test_workdir_error_does_not_publish_the_setup_gate(self):
        self.run_pipeline(preflight_fails=True)

    def run_pipeline(self, *, disconnect=False, setup_fails=False, cancel_owner=False, cancel_client=False,
                     release_fails=False, native_env=False, slow_stderr=False, payload_status=255,
                     preflight_fails=False, timings=False):
        with tempfile.TemporaryDirectory(prefix="ocp-", dir="/tmp") as directory:
            root = Path(directory)
            if release_fails:
                (root / "fail-release").touch()
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
            if slow_stderr:
                config["setup"]["independent"]["teardown"] = [
                    sys.executable, "-c",
                    'import sys; from pathlib import Path; Path("teardown").touch(); '
                    'sys.stderr.buffer.write(b"x" * (1024 * 1024))',
                ]
            if preflight_fails:
                (root / "file").touch()
                config["workdir"] = str(root / "file" / "child")
            owner = subprocess.Popen(
                [sys.executable, "-c", DRIVE_FIXTURE, drive, "0", json.dumps(config), "unused"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            client = None
            try:
                if preflight_fails:
                    out, err = owner.communicate(timeout=5)
                    self.assertNotIn(SETUP_LISTENING, out)
                    self.assertIn(b"[Remote preparation failed]", err)
                    self.assertTrue((root / "unassigned").exists())
                    return
                self.assertTrue(select.select([owner.stdout], [], [], 10)[0])
                self.assertEqual(owner.stdout.readline(), SETUP_LISTENING)
                spawn = (
                    'import os, pathlib, sys; '
                    'assert pathlib.Path("mount-started").exists(); '
                    'assert not pathlib.Path("owner-cleaned").exists(); '
                    'assert os.environ["SETUP_VALUE"] == "early"; '
                    'assert os.environ["COMMAND_VALUE"] == "late"; '
                    'assert os.environ["COLAB_DRIVE"] == "/mock/drive"; '
                    f'print(sys.stdin.read(), end=""); raise SystemExit({payload_status})'
                )
                gate = embedded_gate(inspect.getsource(ssh_pipeline),
                                     tuple(inspect.getsource(fn) for fn in (format_duration, display_label, SetupTimeline, completed_steps, setup_summary_socket, progress)))
                command = ["/bin/sh", "-c", 'test "$PYTHONHOME" = /nonexistent && cat; exit 255'] if native_env else [
                    sys.executable, "-c", spawn]
                if cancel_owner:
                    command = ["/bin/sh", "-c", '(sleep .8; touch command-escaped) & touch command-active; wait']
                client = subprocess.Popen(
                    [sys.executable, "-c", gate, control, "0", json.dumps({"timings": timings}), *command],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                command_env = {"COMMAND_VALUE": "late"}
                if native_env:
                    command_env["PYTHONHOME"] = "/nonexistent"
                owner.stdin.write(packet({"SETUP_VALUE": "early"}, command_env))
                owner.stdin.flush()
                wait_file(root / "independent")
                wait_file(root / "prerequisites")
                self.assertFalse((root / "mount-started").exists())
                self.assertIsNone(client.poll())
                early_error = b""
                if setup_fails:
                    # In a pipe, don't hold an already failed task's error
                    # until independent work (here the Drive config) finishes.
                    self.assertTrue(select.select([client.stderr], [], [], 5)[0])
                    early_error = client.stderr.readline()
                    self.assertIn(b"[Setup failed] Independent setup (exit 7).", early_error)
                if cancel_client:
                    wait_file(root / "control-attached")
                    client.send_signal(signal.SIGINT)
                    self.assertEqual(client.communicate(timeout=10), (b"", b"[cancelled]\n"))
                    owner.wait(timeout=10)
                    self.assertEqual((client.returncode, owner.returncode), (130, 130))
                    self.assertTrue((root / "unassigned").exists())
                    self.assertFalse((root / "mount-started").exists())
                    return
                if disconnect:
                    wait_file(root / "control-attached")
                    client.kill()
                    client.communicate(timeout=5)
                    owner.wait(timeout=8)
                    self.assertTrue((root / "owner-cleaned").exists())
                    self.assertTrue((root / "unassigned").exists())
                    self.assertFalse((root / "mount-started").exists())
                    return
                if timings:
                    # Make config, not process teardown, the final blocker.
                    wait_file(root / "independent-finished")
                owner.stdin.write(b'"cHJpdmF0ZSBjb25maWd1cmF0aW9u"\n')
                owner.stdin.flush()
                if cancel_owner:
                    wait_file(root / "command-active")
                    owner.terminate()
                    _, err = client.communicate(timeout=8)
                    self.assertEqual((client.returncode, err), (143, b""))
                    owner.wait(timeout=8)
                    self.assertEqual(owner.returncode, 143)
                    self.assertTrue((root / "unassigned").exists())
                    time.sleep(.9)
                    self.assertFalse((root / "command-escaped").exists())
                    return
                if slow_stderr:
                    client.stdin.write(b"untouched stdin\n")
                    client.stdin.close()
                    client.stdin = None
                    wait_file(root / "teardown")
                    time.sleep(3.2)
                    self.assertFalse((root / "unassigned").exists())
                    out, err = client.communicate(timeout=15)
                    self.assertEqual(err, b"x" * (1024 * 1024))
                    err = b""
                else:
                    out, err = client.communicate(b"untouched stdin\n", timeout=15)
                if release_fails:
                    self.assertEqual((client.returncode, out), (125, b"untouched stdin\n"))
                    self.assertIn(b"[VM release unconfirmed]", err)
                elif setup_fails:
                    self.assertEqual((client.returncode, out), (7, b""))
                    self.assertEqual(early_error + err, b"[Setup failed] Independent setup (exit 7).\n")
                else:
                    self.assertEqual((client.returncode, out), (payload_status, b"untouched stdin\n"))
                    if timings:
                        for label in ("Setting up…", "Receiving Drive config…", "Starting Drive mounts…",
                                      "Mounting work…"):
                            self.assertIn(("✓ " + label).encode(), err)
                        self.assertNotIn(b"Independent setup", err)
                    else:
                        self.assertEqual(err, b"")
                _, err = owner.communicate(timeout=10)
                expected = 125 if release_fails else (7 if setup_fails else payload_status)
                self.assertEqual((owner.returncode, err), (expected, b""))
                self.assertTrue((root / "owner-cleaned").exists())
                self.assertEqual((root / "unassigned").exists(), not release_fails)
            finally:
                for process in (client, owner):
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.communicate()
