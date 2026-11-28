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
    def __init__(self, message, *, retry=False, delay=0, uncertain=False):
        super().__init__(message)
        self.retry = retry
        self.delay = delay
        self.uncertain = uncertain


def emit(**event):
    print(json.dumps(event), flush=True)


def retry_after(value):
    if value.isascii() and value.isdigit():
        return min(86400, int(value[:10])) if len(value) <= 10 else 86400
    try:
        return min(86400, max(0, math.ceil(parsedate_to_datetime(value).timestamp() - datetime.now().timestamp())))
    except (TypeError, ValueError, OverflowError):
        return 0


def connect():
    try:
        from colab_cli import auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise Failure("Cannot load Colab CLI authentication. Install or update 'google-colab-cli' with uv.") from None

    def need_login(*args, **kwargs):
        raise Failure(LOGIN)

    # Reuse keyring lookup and refresh, but never hide an interactive sign-in
    # behind the allocator display or read the user's command stdin.
    original = auth._run_remote_flow
    auth._run_remote_flow = need_login
    try:
        session = auth.get_credentials()
        return AuthorizedSession(session.credentials, max_refresh_attempts=0)
    finally:
        auth._run_remote_flow = original


class Allocation:
    def __init__(self, session, accelerator, variant, high_mem):
        self.session = session
        self.params = {"nbh": str(uuid4()).replace("-", "_").ljust(44, "."),
                       "variant": variant, "accelerator": accelerator}
        if high_mem and accelerator not in {"L4", "V5E1", "V6E1"}:
            self.params["shape"] = "hm"
        self.target = accelerator if variant != "DEFAULT" else ("CPU High-RAM" if high_mem else "CPU")
        self.endpoint = None
        self.uncertain = False

    def request(self, method, path, *, params=None, token=None, timeout=12):
        from google.auth.exceptions import RefreshError, TransportError
        from requests.exceptions import ConnectionError, Timeout

        headers = {"Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"}
        if token:
            headers["X-Goog-Colab-Token"] = token
        try:
            response = self.session.request(method, API + path, params=params, headers=headers,
                                            timeout=(min(4, timeout), timeout), max_allowed_time=timeout,
                                            allow_redirects=False)
        except RefreshError:
            raise Failure(LOGIN, uncertain=method == "POST") from None
        except (Timeout, ConnectionError, TransportError) as exc:
            message = "Colab request timed out" if isinstance(exc, Timeout) else "Could not reach Google Colab"
            raise Failure(message, retry=True, uncertain=method == "POST") from None
        code = response.status_code
        if not 200 <= code < 300:
            reason = {400: "invalid request or accelerator not available to this account",
                      401: LOGIN, 403: "Access denied; check the signed-in account",
                      412: "Allocation refused (session, usage or capacity limit); check 'colab sessions'",
                      429: "Colab is rate limiting requests", 502: "Colab gateway error",
                      503: "Colab service unavailable", 504: "Colab gateway timed out"}.get(code, "unexpected response")
            raise Failure(f"HTTP {code}: {reason}", retry=code in (429, 502, 503, 504),
                          delay=retry_after(response.headers.get("Retry-After", "")),
                          uncertain=method == "POST" and code >= 500)
        raw = response.text.removeprefix(")]}'\n")
        try:
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise Failure("Google Colab returned an unexpected response",
                          uncertain=method == "POST") from None
        return data

    def lookup(self, timeout=12):
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

    def run(self, attempts):
        number = 0
        while True:
            number += 1
            count = f"{number}/{attempts}" if attempts else str(number)
            emit(label=f"Creating Colab VM… {self.target} · attempt {count}")
            try:
                return self.attempt()
            except Failure as exc:
                if not exc.retry or (attempts and number >= attempts):
                    raise
                delay = max(exc.delay, min(30, 2 ** min(number + 1, 5)))
                emit(label=f"Waiting to retry · {exc}", deadline=monotonic() + delay)
                sleep(delay)

    def cleanup(self):
        if not self.endpoint and not self.uncertain:
            return
        emit(label="Cancelling allocation… checking Colab")
        if not self.endpoint:
            for _ in range(2):
                try:
                    self.lookup(timeout=2)
                except Failure:
                    pass
                if self.endpoint:
                    break
                sleep(.3)
        if not self.endpoint:
            emit(error="Could not confirm whether a VM was allocated; check 'colab sessions'.")
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
    p = urlsplit(url) if isinstance(url, str) else None
    if (not p or p.scheme != "https" or not p.hostname or not p.hostname.endswith(".colab.dev")
            or p.username is not None or p.password is not None or not isinstance(token, str) or not token
            or type(duration) is not int or not 1 <= duration <= 86400):
        raise Failure("Google Colab returned invalid SSH connection details")
    state = dict(name=name, endpoint=allocation.endpoint, token=token, url=url,
                 token_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat(),
                 variant=allocation.params["variant"], accelerator=allocation.params["accelerator"],
                 machine_shape="HIGH_RAM" if allocation.params.get("shape") == "hm" else "STANDARD")
    with os.fdopen(os.open(store, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        json.dump({name: state}, out)


def debug_error(exc, debug):
    if debug:
        from traceback import extract_tb
        # No exception repr, request URL, response body or frame locals.
        emit(debug=type(exc).__name__)
        for frame in extract_tb(exc.__traceback__):
            emit(debug=f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")


def main():
    store, name, accelerator, variant, high_mem, attempts, debug = sys.argv[1:]
    backend = None
    status = 1
    try:
        emit(label="Checking Colab sign-in…")
        backend = Allocation(connect(), accelerator, variant, high_mem == "1")
        result = backend.run(int(attempts))
        save_session(store, name, result, backend)
        return 0
    except KeyboardInterrupt:
        status = 130
    except Failure as exc:
        emit(error=str(exc))
        debug_error(exc, debug == "1")
    except Exception as exc:
        emit(error="Unexpected error while creating a Colab VM. Use --debug for details.")
        debug_error(exc, debug == "1")
    if backend:
        signal(SIGINT, SIG_IGN)
        try:
            backend.cleanup()
        except Exception as exc:
            emit(error="Could not verify release of the allocated VM; check 'colab sessions'.")
            debug_error(exc, debug == "1")
    return status


if __name__ == "__main__":
    sys.exit(main())
