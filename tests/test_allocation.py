import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

from allocator_fixture import fake_colab
from oncolab import allocation_worker as worker


class Protocol(unittest.TestCase):
    def test_auth_flow_is_not_entered_when_credentials_are_missing(self):
        hidden_login = Mock(side_effect=AssertionError("unexpected interactive login"))
        auth = SimpleNamespace(_run_remote_flow=hidden_login)
        auth.get_credentials = lambda: auth._run_remote_flow({})
        with self.assertRaisesRegex(worker.Failure, "colab sessions"):
            worker.credential_session(auth, Mock())
        hidden_login.assert_not_called()
        self.assertIs(auth._run_remote_flow, hidden_login)

    def test_wire_parameters_and_http_errors(self):
        class NetworkException(Exception):
            pass
        exceptions = SimpleNamespace(RefreshError=type("RefreshError", (Exception,), {}),
                                     TransportError=NetworkException)
        requests = SimpleNamespace(Timeout=TimeoutError, ConnectionError=ConnectionError)
        with patch.dict(sys.modules, {"google.auth.exceptions": exceptions, "requests.exceptions": requests}):
            session = Mock()
            session.request.side_effect = [
                SimpleNamespace(status_code=503, headers={"Retry-After": "9"}),
                SimpleNamespace(status_code=400, headers={}),
                TimeoutError(),
            ]
            backend = worker.Allocation(session, "T4", "GPU", True)
            with self.assertRaises(worker.Failure) as refusal:
                backend.request("POST", "/assign", params=backend.params, token="xsrf")
            self.assertEqual((refusal.exception.retry, refusal.exception.delay, refusal.exception.uncertain),
                             (True, 9, True))
            call = session.request.call_args
            self.assertEqual(call.kwargs["params"], {"authuser": "0", **backend.params})
            self.assertEqual(call.kwargs["headers"]["X-Goog-Colab-Token"], "xsrf")
            self.assertFalse(call.kwargs["allow_redirects"])
            with self.assertRaises(worker.Failure) as refusal:
                backend.request("POST", "/assign")
            self.assertFalse(refusal.exception.retry)
            with self.assertRaises(worker.Failure) as refusal:
                backend.request("POST", "/assign")
            self.assertTrue(refusal.exception.uncertain)

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
