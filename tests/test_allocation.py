import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

from allocator_fixture import fake_colab
from oncolab import allocation_worker as worker, cli


class Protocol(unittest.TestCase):
    def test_auth_flow_is_not_entered_when_credentials_are_missing(self):
        hidden_login = Mock(side_effect=AssertionError("unexpected interactive login"))
        auth = SimpleNamespace(_run_remote_flow=hidden_login)
        auth.get_credentials = lambda: auth._run_remote_flow({})
        with self.assertRaisesRegex(worker.Failure, "colab sessions"):
            worker.credential_session(auth, Mock())
        hidden_login.assert_not_called()
        self.assertIs(auth._run_remote_flow, hidden_login)

    def test_auth_refresh_is_available_to_read_requests_only(self):
        credentials = object()
        auth = SimpleNamespace(_run_remote_flow=Mock(),
                               get_credentials=lambda: SimpleNamespace(credentials=credentials))
        new_session = Mock()
        reader, writer = worker.credential_session(auth, new_session)
        self.assertIs(reader.credentials, credentials)
        self.assertIs(writer, new_session.return_value)
        new_session.assert_called_once_with(credentials, max_refresh_attempts=0)

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
            self.assertIn("rejected T4", str(refusal.exception))
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
            {"token": "not-visible"}, {"token": "still-not-visible"},
            {"endpoint": "own-vm"}, {"token": "release"}, {},
        ]) as request, patch.object(worker, "emit"), patch.object(worker, "sleep")):
            backend.cleanup()
        self.assertEqual([c.args for c in request.call_args_list],
                         [("GET", "/assign")] * 3 + [("GET", "/unassign/own-vm"), ("POST", "/unassign/own-vm")])


    def test_sigint_at_successful_handoff_releases_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions.json"
            store.write_text("{}")
            installed = []

            def change_handler(number, handler):
                installed.append(handler)
                if len(installed) == 2:
                    # The last signal delivered to the allocator's handler
                    # arrives just before restoring the previous handler.
                    installed[0]()
                return signal.default_int_handler

            with (patch.object(cli, "signal", side_effect=change_handler),
                  patch.object(cli, "run_allocator", return_value=0),
                  patch.object(cli, "release") as release,
                  patch.object(cli, "cancel_footer")):
                self.assertEqual(cli.allocate(None, False, False, store, ()), (None, 130))
            release.assert_called_once()


class InstalledCLI(unittest.TestCase):
    def run_cli(self, mode, tmp, debug=False):
        fake_colab(Path(tmp))
        env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", XDG_CONFIG_HOME=tmp, TEST_ALLOCATION=mode,
                   TEST_ALLOCATION_READY=str(Path(tmp) / "ready"))
        flags = ["--debug"] if debug else []
        return subprocess.Popen([which("oncolab"), *flags, "--", "true"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_noninteractive_login_fails_with_instruction(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("login", tmp) as proc:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(out, "")
            self.assertIn("Run 'colab sessions'", err)
            self.assertNotIn("Traceback", err)

    def test_allocator_crash_is_actionable_without_raw_traceback(self):
        for debug in (False, True):
            with tempfile.TemporaryDirectory() as tmp, self.run_cli("crash", tmp, debug) as proc:
                out, err = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(out, "")
                self.assertIn("The Colab allocator exited unexpectedly", err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("secret-token", err)
                self.assertEqual("auth.py:12 in open_session" in err, debug)

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


    def test_closed_diagnostic_pipe_does_not_strand_allocator(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("cancel", tmp) as proc:
            proc.stderr.close()
            ready = Path(tmp) / "ready"
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.is_file())
                proc.send_signal(signal.SIGINT)
                self.assertEqual(proc.wait(timeout=5), 130)
            finally:
                if proc.poll() is None:
                    proc.kill()
