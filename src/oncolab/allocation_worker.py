"""Run with the installed Colab CLI's Python so OAuth stays in its environment."""
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from signal import SIGINT, SIG_IGN, signal
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

API = "https://colab.research.google.com/tun/m"
LOGIN = "No usable Colab credentials. Run 'colab sessions' in a terminal to sign in."


class Failure(Exception):
    def __init__(self, message, *, retry=False, delay=0, uncertain=False, detail=None, retry_after_hint=False):
        super().__init__(message)
        self.retry = retry
        self.delay = delay
        self.uncertain = uncertain
        self.detail = detail or message
        self.retry_after_hint = retry_after_hint


def emit(**event):
    try:
        # Use unbuffered writes so a closed pipe cannot derail VM cleanup.
        os.write(sys.stdout.fileno(), (json.dumps(event) + "\n").encode())
    except OSError:
        pass


def retry_after(value):
    if value.isascii() and value.isdigit():
        return int(value) if len(value) <= 12 else math.inf
    try:
        return max(0, math.ceil(parsedate_to_datetime(value).timestamp() - datetime.now().timestamp()))
    except (TypeError, ValueError, OverflowError):
        return 0


def connect():
    try:
        from colab_cli import auth
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise Failure("Cannot load Colab CLI authentication. Install or update 'google-colab-cli' with uv.") from None

    return credential_session(auth, AuthorizedSession, RefreshError, TransportError)


def identity_unavailable():
    return Failure("Colab sign-in service is temporarily unavailable", retry=True,
                   detail="sign-in service unavailable")


def credential_session(auth, authorized_session, refresh_error=(), transport_error=()):
    temporary_failure = False

    def need_login(*args, **kwargs):
        raise identity_unavailable() if temporary_failure else Failure(LOGIN)

    # Colab catches failures refreshing expired cached credentials before
    # calling its login flow. Preserve a transient failure, without asking
    # for input or rewriting its credential/keyring implementation.
    credential_type = getattr(auth, "Credentials", None)
    refresh = getattr(credential_type, "refresh", None)

    def observe_refresh(credentials, request):
        nonlocal temporary_failure
        try:
            return refresh(credentials, request)
        except refresh_error as exc:
            temporary_failure = exc.retryable
            raise
        except transport_error:
            temporary_failure = True
            raise

    original = auth._run_remote_flow
    auth._run_remote_flow = need_login
    if refresh is not None:
        credential_type.refresh = observe_refresh
    try:
        session = auth.get_credentials()
        # Lookup GETs can refresh and resend authentication just as colab
        # does. A write must never be implicitly replayed by the library.
        return session, authorized_session(session.credentials, max_refresh_attempts=0)
    finally:
        auth._run_remote_flow = original
        if refresh is not None:
            credential_type.refresh = refresh


class Allocation:
    def __init__(self, session, accelerator, variant, high_mem, request_timeout=120, *,
                 write_session=None, authenticate=None):
        self.session = session
        self.authenticate = authenticate
        self.write_session = session if write_session is None else write_session
        self.request_timeout = request_timeout
        self.params = {"nbh": str(uuid4()).replace("-", "_").ljust(44, "."),
                       "variant": variant, "accelerator": accelerator}
        if high_mem and accelerator not in {"L4", "V5E1", "V6E1"}:
            self.params["shape"] = "hm"
        self.target = "CPU" if variant == "DEFAULT" else accelerator
        if self.params.get("shape") == "hm":
            self.target += " High-RAM"
        self.endpoint = None
        self.uncertain = False
        self.reconcile_after = 0

    def request(self, method, path, *, params=None, token=None, timeout=None):
        from google.auth.exceptions import RefreshError, TransportError
        from requests.exceptions import ChunkedEncodingError, ConnectionError, ProxyError, SSLError, Timeout

        timeout = self.request_timeout if timeout is None else timeout
        headers = {"Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"}
        if token:
            headers["X-Goog-Colab-Token"] = token
        try:
            transport = self.write_session if method == "POST" else self.session
            response = transport.request(method, API + path, params={"authuser": "0", **(params or {})}, headers=headers,
                                            timeout=(min(4, timeout), timeout), max_allowed_time=timeout,
                                            allow_redirects=False)
        except RefreshError as exc:
            if exc.retryable:
                failure = identity_unavailable()
                failure.uncertain = method == "POST"
                raise failure from None
            raise Failure(LOGIN, uncertain=method == "POST") from None
        except SSLError:
            raise Failure("Could not establish secure TLS with Colab; check the trusted certificates.",
                          uncertain=method == "POST") from None
        except (Timeout, ConnectionError, TransportError, ChunkedEncodingError) as exc:
            if isinstance(exc, ProxyError) and "407" in str(exc):
                raise Failure("Your network proxy requires authentication; check its configuration.") from None
            message = "Colab request timed out" if isinstance(exc, Timeout) else "Could not reach Google Colab"
            raise Failure(message, retry=True, uncertain=method == "POST",
                          detail="time out" if isinstance(exc, Timeout) else "network error") from None
        code = response.status_code
        if not 200 <= code < 300:
            reason = {400: "invalid request or accelerator not available to this account",
                      401: LOGIN, 403: "Access denied; check the signed-in account",
                      412: "Allocation refused (session, usage or capacity limit); check 'colab sessions'",
                      429: "Colab is rate limiting requests", 502: "Colab gateway error",
                      503: "Colab service unavailable", 504: "Colab gateway timed out"}.get(code, "unexpected response")
            if code == 400 and path == "/assign" and self.params["variant"] != "DEFAULT":
                reason = f"Colab rejected {self.params['accelerator']}; try another accelerator or omit --gpu/--tpu"
            retry_details = {502: "502 gateway error", 503: "503 unavailable", 504: "504 gateway timeout"}
            delay = retry_after(response.headers.get("Retry-After", ""))
            if path == "/assign" and delay and (code in retry_details or code == 429):
                self.reconcile_after = max(self.reconcile_after, monotonic() + delay)
            if not math.isfinite(delay):
                raise Failure(f"HTTP {code}: {reason}; server holdoff is too large to schedule",
                              uncertain=method == "POST" and code >= 500)
            show_holdoff = code == 429 or delay > 120
            if show_holdoff and not delay:
                reason += "; wait before trying again"
            # Respect a long server holdoff without making a normal command
            # wait for hours, and never start another request before it ends.
            raise Failure(f"HTTP {code}: {reason}", retry=code in retry_details,
                          delay=delay, retry_after_hint=show_holdoff and bool(delay),
                          uncertain=method == "POST" and code >= 500, detail=retry_details.get(code))
        raw = response.text.removeprefix(")]}'\n")
        try:
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise Failure("Google Colab returned an unexpected response",
                          uncertain=method == "POST") from None
        return data

    def lookup(self, timeout=None):
        data = self.request("GET", "/assign", params=self.params, timeout=timeout)
        endpoint = data.get("endpoint")
        if endpoint is not None:
            if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", endpoint):
                raise Failure("Google Colab returned an invalid VM ID")
            self.endpoint = endpoint
        return data

    def attempt(self):
        # Always reconcile against the same allocation ID before another POST.
        data = self.lookup()
        if self.endpoint:
            return data
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise Failure("Google Colab did not return an allocation token")
        previous = self.uncertain
        self.uncertain = True
        try:
            data = self.request("POST", "/assign", params=self.params, token=token)
        except Failure as exc:
            self.uncertain = previous or exc.uncertain
            raise
        endpoint = data.get("endpoint")
        if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", endpoint):
            raise Failure("Google Colab did not return a usable VM ID")
        self.endpoint = endpoint
        return data

    def run(self, attempts, backoff=10):
        number = 0
        while True:
            number += 1
            count = f"{number}/{attempts}" if attempts else str(number)
            prefix = f"{self.target} · attempt {count}"
            try:
                if self.authenticate is not None:
                    emit(label="Checking Colab sign-in…", detail=f"attempt {count}")
                    self.session, self.write_session = self.authenticate()
                    self.authenticate = None
                emit(label="Creating Colab VM…", prefix=prefix)
                return self.attempt()
            except Failure as exc:
                if not exc.retry or (attempts and (number >= attempts or exc.delay > 120)):
                    raise
                delay = max(exc.delay, min(120, backoff * 2 ** min(number - 1, 16)))
                emit(label="Waiting to retry…", prefix=prefix, detail=exc.detail, deadline=monotonic() + delay)
                # Keep each platform sleep well within its system time range.
                while delay > 3600:
                    sleep(3600)
                    delay -= 3600
                sleep(delay)

    def cleanup(self):
        if not self.endpoint and not self.uncertain:
            return
        emit(label="Checking Colab VM…", detail="checking whether allocation completed")
        if not self.endpoint:
            # Allow late allocation to become visible, then reserve time
            # for both release calls inside the supervisor's 11-second grace.
            deadline = monotonic() + 5.5
            while (remaining := deadline - monotonic()) > 0:
                holdoff = self.reconcile_after - monotonic()
                if holdoff >= remaining:
                    break
                if holdoff > 0:
                    sleep(holdoff)
                    continue
                try:
                    self.lookup(timeout=min(1.5, remaining))
                except Failure:
                    pass
                if self.endpoint:
                    break
                sleep(max(0, min(.45, deadline - monotonic())))
        if not self.endpoint:
            emit(error="Could not confirm allocation; check 'colab sessions'.", unconfirmed=True)
            return
        path = "/unassign/" + self.endpoint
        release = self.request("GET", path, timeout=2)
        token = release.get("token")
        if not isinstance(token, str) or not token:
            raise Failure("Google Colab did not return a release token")
        self.request("POST", path, token=token, timeout=2)


def save_session(store, name, result, allocation):
    info = result.get("runtimeProxyInfo")
    if not isinstance(info, dict):
        raise Failure("Google Colab did not return SSH connection details")
    url, token, duration = (info.get(k) for k in ("url", "token", "tokenExpiresInSeconds"))
    if isinstance(duration, str) and re.fullmatch(r"[0-9]{1,5}", duration):
        duration = int(duration)
    p = urlsplit(url) if isinstance(url, str) else None
    if (not p or p.scheme != "https" or not p.hostname or not p.hostname.endswith(".colab.dev")
            or p.username is not None or p.password is not None or not isinstance(token, str) or not token
            or type(duration) is not int or not 1 <= duration <= 86400):
        raise Failure("Google Colab returned invalid SSH connection details")
    state = dict(name=name, endpoint=allocation.endpoint, token=token, url=url,
                 token_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat(),
                 variant=allocation.params["variant"], accelerator=allocation.params["accelerator"],
                 machine_shape="HIGH_RAM" if allocation.params.get("shape") == "hm" else "STANDARD")
    pending = Path(store).with_suffix(".tmp")
    with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        json.dump({name: state}, out)
    pending.replace(store)


def debug_error(exc, debug):
    if debug:
        from traceback import extract_tb
        # No exception repr, request URL, response body or frame locals.
        emit(debug=type(exc).__name__)
        for frame in extract_tb(exc.__traceback__):
            emit(debug=f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")


def main():
    store, name, accelerator, variant, high_mem, attempts, debug, timeout, backoff = sys.argv[1:]
    backend = None
    status = 1
    try:
        try:
            backend = Allocation(None, accelerator, variant, high_mem == "1", float(timeout),
                                 authenticate=connect)
            result = backend.run(int(attempts), float(backoff))
            save_session(store, name, result, backend)
            status = 0
        finally:
            # Stop taking SIGINT before reporting an error, not after it.
            # Otherwise a cancellation during error reporting can bypass
            # reconciliation of a POST whose result is still uncertain.
            signal(SIGINT, SIG_IGN)
    except KeyboardInterrupt:
        status = 130
    except Failure as exc:
        if exc.retry_after_hint:
            emit(error=str(exc), retry_after=exc.delay)
        else:
            emit(error=str(exc))
        debug_error(exc, debug == "1")
    except Exception as exc:
        emit(error="Unexpected error while creating a Colab VM. Use --debug for details.")
        debug_error(exc, debug == "1")
    finally:
        if backend and status != 0:
            try:
                backend.cleanup()
            except Exception as exc:
                emit(error=f"Could not verify release of VM {backend.endpoint!r}; check 'colab sessions'.", unconfirmed=True)
                debug_error(exc, debug == "1")
            finally:
                Path(store).unlink(missing_ok=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
