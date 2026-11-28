import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from shutil import which
from unittest.mock import patch

from allocator_fixture import fake_colab
from oncolab import allocation_worker as worker


class Protocol(unittest.TestCase):
    def test_uncertain_post_reconciles_same_id_before_retry(self):
        backend = worker.Allocation(None, "A100", "GPU", True)
        with (patch.object(backend, "request", side_effect=[
            {"token": "assignment"}, worker.Failure("timeout", retry=True, uncertain=True),
            {"endpoint": "existing"},
        ]) as request, patch.object(worker, "emit") as emit, patch.object(worker, "sleep")):
            self.assertEqual(backend.run(4), {"endpoint": "existing"})
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET", "POST", "GET"])
        self.assertEqual(len({c.kwargs["params"]["nbh"] for c in request.call_args_list}), 1)
        self.assertEqual(backend.params["shape"], "hm")
        self.assertTrue(any("Waiting to retry" in c.kwargs["label"] for c in emit.call_args_list))

    def test_final_refusal_does_not_sleep(self):
        backend = worker.Allocation(None, "V6E1", "TPU", True)
        self.assertNotIn("shape", backend.params)
        with (patch.object(backend, "attempt", side_effect=worker.Failure("HTTP 412")) as attempt,
              patch.object(worker, "emit"), patch.object(worker, "sleep") as sleep):
            with self.assertRaisesRegex(worker.Failure, "HTTP 412"):
                backend.run(0)
        attempt.assert_called_once()
        sleep.assert_not_called()

    def test_cancellation_cleanup_releases_only_the_matching_vm(self):
        backend = worker.Allocation(None, "NONE", "DEFAULT", False)
        backend.uncertain = True
        with (patch.object(backend, "request", side_effect=[
            {"endpoint": "own-vm"}, {"token": "release"}, {},
        ]) as request, patch.object(worker, "emit")):
            backend.cleanup()
        self.assertEqual([c.args for c in request.call_args_list],
                         [("GET", "/assign"), ("GET", "/unassign/own-vm"), ("POST", "/unassign/own-vm")])


class InstalledCLI(unittest.TestCase):
    def run_cli(self, mode, tmp):
        fake_colab(Path(tmp))
        env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", XDG_CONFIG_HOME=tmp, TEST_ALLOCATION=mode)
        return subprocess.Popen([which("oncolab"), "--", "true"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_noninteractive_login_fails_with_instruction(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("login", tmp) as proc:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("Run 'colab sessions'", err)
            self.assertNotIn("Traceback", err)

    def test_sigint_during_backoff_exits_promptly(self):
        import select
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("cancel", tmp) as proc:
            try:
                self.assertTrue(select.select([proc.stderr], [], [], 8)[0])
                self.assertIn("TEST_BACKOFF_STARTED", proc.stderr.readline())
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 130)
                self.assertEqual(out, "")
                self.assertIn("[cancelled]", err)
                self.assertNotIn("Traceback", err)
            finally:
                if proc.poll() is None:
                    proc.kill()
