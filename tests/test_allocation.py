import json
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
from oncolab import allocation, allocation_worker as worker, cli


class Protocol(unittest.TestCase):
    def test_default_retry_starts_at_one_second_and_respects_server_delay(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["oncolab", "true"]):
            _, args, _ = cli.parse_args()
        self.assertEqual(args.retry_backoff, 1)
        allocator = worker.Allocation(None, "NONE", "DEFAULT", False)
        allocator.attempt = Mock(side_effect=[
            worker.Failure("temporary", retry=True),
            worker.Failure("temporary", retry=True, delay=9),
            {"endpoint": "fixture"},
        ])
        with patch.object(worker, "sleep") as sleep, patch.object(worker, "emit"):
            self.assertEqual(allocator.run(4), {"endpoint": "fixture"})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 9])

    def test_auth_flow_is_not_entered_when_credentials_are_missing(self):
        hidden_login = Mock(side_effect=AssertionError("unexpected interactive login"))
        auth = SimpleNamespace(_run_remote_flow=hidden_login)
        auth.get_credentials = lambda: auth._run_remote_flow({})
        with self.assertRaisesRegex(worker.Failure, "colab sessions"):
            worker.credential_session(auth, Mock())
        hidden_login.assert_not_called()
        self.assertIs(auth._run_remote_flow, hidden_login)

    def test_expired_credentials_with_transient_refresh_failure_are_retried(self):
        class RefreshError(Exception):
            retryable = True

        class Credentials:
            def refresh(self, request):
                raise RefreshError()

        refresh = Credentials.refresh
        auth = SimpleNamespace(Credentials=Credentials, _run_remote_flow=Mock())

        def load():
            try:
                Credentials().refresh(None)
            except RefreshError:
                return auth._run_remote_flow({})
            self.fail("refresh should fail")

        auth.get_credentials = load
        backend = worker.Allocation(None, "NONE", "DEFAULT", False, authenticate=Mock(side_effect=[
            worker.Failure("transient", retry=True),
            (None, None),
        ]))
        with self.assertRaisesRegex(worker.Failure, "sign-in service is temporarily unavailable") as observed:
            worker.credential_session(auth, Mock(), RefreshError)
        self.assertTrue(observed.exception.retry)
        self.assertIs(Credentials.refresh, refresh)
        backend.authenticate.side_effect = [observed.exception, (None, None)]
        with (patch.object(backend, "attempt", return_value={"endpoint": "test"}) as attempt,
              patch.object(worker, "sleep") as sleep, patch.object(worker, "emit")):
            self.assertEqual(backend.run(2), {"endpoint": "test"})
        attempt.assert_called_once()
        sleep.assert_called_once_with(1)

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
        class RefreshError(Exception):
            retryable = True
        exceptions = SimpleNamespace(RefreshError=RefreshError, TransportError=NetworkException)
        class TruncatedResponse(Exception):
            pass
        class CertificateError(ConnectionError):
            pass
        class ProxyError(ConnectionError):
            pass
        requests = SimpleNamespace(Timeout=TimeoutError, ConnectionError=ConnectionError,
                                   ChunkedEncodingError=TruncatedResponse, ProxyError=ProxyError,
                                   SSLError=CertificateError)
        with patch.dict(sys.modules, {"google.auth.exceptions": exceptions, "requests.exceptions": requests}):
            session = Mock()
            session.request.side_effect = [
                SimpleNamespace(status_code=503, headers={"Retry-After": "9"}),
                SimpleNamespace(status_code=400, headers={}),
                TimeoutError(),
                TruncatedResponse(),
                CertificateError("certificate verify failed"),
                ProxyError("Tunnel connection failed: 407 Proxy Authentication Required"),
                RefreshError("temporary identity backend error"),
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
            for _ in range(2):
                with self.assertRaises(worker.Failure) as refusal:
                    backend.request("POST", "/assign")
                self.assertTrue(refusal.exception.uncertain)
                self.assertTrue(refusal.exception.retry)

            for hint in ("trusted certificates", "proxy requires authentication"):
                with self.assertRaisesRegex(worker.Failure, hint) as refusal:
                    backend.request("POST", "/assign")
                self.assertFalse(refusal.exception.retry)

            with self.assertRaisesRegex(worker.Failure, "sign-in service is temporarily unavailable") as refusal:
                backend.request("GET", "/assign")
            self.assertTrue(refusal.exception.retry)

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
        waiting, = [c.kwargs for c in emit.call_args_list if c.kwargs["label"] == "Waiting to retry…"]
        self.assertEqual(waiting["detail"], "timeout")

    def test_creating_and_waiting_show_the_same_requested_target_and_attempt(self):
        for accelerator, variant, high_mem, target in (
            ("NONE", "DEFAULT", False, "CPU"),
            ("NONE", "DEFAULT", True, "CPU High-RAM"),
            ("T4", "GPU", False, "T4"),
            ("T4", "GPU", True, "T4 High-RAM"),
            ("L4", "GPU", True, "L4"),
            ("V5E1", "TPU", True, "V5E1"),
        ):
            with self.subTest(target=target):
                backend = worker.Allocation(None, accelerator, variant, high_mem)
                with (patch.object(backend, "attempt", side_effect=[
                    worker.Failure("HTTP 503", retry=True, detail="503 unavailable"),
                    {"endpoint": "test"},
                ]), patch.object(worker, "emit") as emit, patch.object(worker, "sleep")):
                    self.assertEqual(backend.run(4), {"endpoint": "test"})
                first, wait, second = [call.kwargs for call in emit.call_args_list]
                self.assertEqual(first, {"label": "Creating Colab VM…", "prefix": f"{target} · attempt 1/4"})
                self.assertEqual(wait["label"], "Waiting to retry…")
                self.assertEqual(wait["prefix"], first["prefix"])
                self.assertEqual(wait["detail"], "503 unavailable")
                self.assertEqual(second, {"label": "Creating Colab VM…", "prefix": f"{target} · attempt 2/4"})

    def test_countdown_preserves_attempt_and_does_not_round_to_zero_early(self):
        label = allocation.AllocationLabel()
        label.state = ("Creating Colab VM…", "", None, "CPU · attempt 3")
        self.assertEqual(label.detail(), "CPU · attempt 3")
        label.state = ("Waiting to retry…", "time out", 100.01, "CPU · attempt 3")
        with patch.object(allocation, "monotonic", return_value=100):
            self.assertEqual(label.detail(), "CPU · attempt 3 · 0.1s · time out")
        with patch.object(allocation, "monotonic", return_value=100.2):
            self.assertEqual(label.detail(), "CPU · attempt 3 · 0.0s · time out")
        label.state = ("Waiting to retry…", "HTTP 503", 90100, "T4 High-RAM · attempt 2/4")
        with patch.object(allocation, "monotonic", return_value=100):
            self.assertEqual(label.detail(), "T4 High-RAM · attempt 2/4 · 1.1d · HTTP 503")

    def test_retry_after_preserves_multi_day_delay(self):
        self.assertEqual(worker.retry_after("172800"), 172800)

    def test_long_server_holdoff_is_waited_only_when_unlimited(self):
        backend = worker.Allocation(None, "NONE", "DEFAULT", False)
        holdoff = worker.Failure("HTTP 503", retry=True, delay=180)
        for attempts in (4, 0):
            with (patch.object(backend, "attempt", side_effect=[holdoff, {"endpoint": "test"}]),
                  patch.object(worker, "sleep") as sleep, patch.object(worker, "emit")):
                if attempts:
                    with self.assertRaises(worker.Failure):
                        backend.run(attempts)
                    sleep.assert_not_called()
                else:
                    self.assertEqual(backend.run(attempts), {"endpoint": "test"})
                    sleep.assert_called_once_with(180)

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


    def test_cancellation_does_not_reconcile_before_server_holdoff(self):
        backend = worker.Allocation(None, "NONE", "DEFAULT", False)
        backend.uncertain = True
        backend.reconcile_after = 280
        with (patch.object(worker, "monotonic", return_value=100),
              patch.object(backend, "lookup") as lookup, patch.object(worker, "emit") as emit):
            backend.cleanup()
        lookup.assert_not_called()
        self.assertIn("check 'colab sessions'", emit.call_args.kwargs["error"])

    def test_cleanup_is_armed_before_reporting_a_failed_post(self):
        backend = Mock()
        backend.run.side_effect = worker.Failure("POST timed out")
        argv = ["worker", "/unused/sessions.json", "test", "NONE", "DEFAULT", "0", "1", "0", "120", "10"]
        observed = []

        def report(**event):
            if "error" in event:
                observed.append(handler.call_args.args)

        with (patch.object(worker.sys, "argv", argv), patch.object(worker, "connect", return_value=(None, None)),
              patch.object(worker, "Allocation", return_value=backend), patch.object(worker, "signal") as handler,
              patch.object(worker, "emit", side_effect=report)):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(observed, [(signal.SIGINT, signal.SIG_IGN)])
        backend.cleanup.assert_called_once()

    def test_session_store_is_private_and_accepts_numeric_string_ttl(self):
        backend = worker.Allocation(None, "NONE", "DEFAULT", False)
        backend.endpoint = "only-test-vm"
        info = dict(url="https://test.colab.dev/", token="proxy-fixture", tokenExpiresInSeconds="3600")
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions.json"
            worker.save_session(store, "only-test-session", {"runtimeProxyInfo": info}, backend)
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(store.read_text())["only-test-session"]["endpoint"], "only-test-vm")

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
                self.assertEqual(cli.allocate(SimpleNamespace(debug=False), False, False, store, ()), (None, 130))
            release.assert_called_once()


class InstalledCLI(unittest.TestCase):
    def run_cli(self, mode, tmp, debug=False, *, stderr=subprocess.PIPE):
        fake_colab(Path(tmp))
        env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", XDG_CONFIG_HOME=tmp, TEST_ALLOCATION=mode,
                   TEST_ALLOCATION_READY=str(Path(tmp) / "ready"))
        flags = ["--debug"] if debug else []
        return subprocess.Popen([which("oncolab"), *flags, "--", "true"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=stderr, text=True)

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

    def test_unconfirmed_vm_reports_one_actionable_outcome_without_color_to_pipe(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("unconfirmed", tmp) as proc:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(out, "")
            self.assertEqual(err, "[VM creation unconfirmed] HTTP 503: Colab service unavailable. "
                                  "Could not confirm allocation; check 'colab sessions'.\n")

    def test_server_holdoff_uses_compact_duration_in_final_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("holdoff", tmp) as proc:
            out, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(out, "")
            self.assertEqual(err, "[VM creation failed] HTTP 429: Colab is rate limiting requests; retry after 1.1d\n")

    def test_sigint_during_backoff_exits_promptly(self):
        with tempfile.TemporaryDirectory() as tmp, self.run_cli("cancel", tmp) as proc:
            try:
                ready = Path(tmp) / "ready"
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.is_file())
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 130)
                self.assertEqual(out, "")
                self.assertEqual(err, "[cancelled]\n")
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


    def test_full_diagnostic_pipe_does_not_hold_failed_allocator(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(writer, b"x" * 4096)
            os.set_blocking(writer, True)
            with tempfile.TemporaryDirectory() as tmp, self.run_cli("login", tmp, stderr=writer) as proc:
                try:
                    self.assertEqual(proc.wait(timeout=5), 1)
                finally:
                    if proc.poll() is None:
                        proc.kill()
        finally:
            os.close(reader)
            os.close(writer)
