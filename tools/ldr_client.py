"""
Local Deep Research (LDR) HTTP client with cookie + CSRF authentication.

LDR does not use a simple API key. It authenticates with a session-cookie /
CSRF-token handshake:

    1. GET  /auth/login  -> parse the <input name="csrf_token"> value from HTML
    2. POST /auth/login  -> form-encoded (username, password, csrf_token)
                             -> sets the `session` cookie
    3. GET  /auth/csrf-token -> {"csrf_token": "..."} -> sent as the
                             `X-CSRF-Token` header on every API call.

This module wraps that handshake and adds retry-with-backoff for the 429
rate-limit responses the server emits when called too quickly.

Configuration comes from environment variables (never hardcoded):

    LDR_BASE_URL   default http://192.168.0.3:1242
    LDR_USERNAME
    LDR_PASSWORD

Usage:
    from ldr_client import LDRClient
    client = LDRClient()
    client.ensure_authenticated()
    data = client.post_json("/api/v1/quick_summary", {"query": "..."})

Session persistence
---------------------
Each LLM tool call is a fresh process, so a module-level cached client does not
survive between calls. To avoid re-authenticating on every call (which trips the
login 429 rate limiter), the client persists the session cookie + CSRF token to
a small JSON file on disk. On the next call the client restores the session and
skips login entirely. Set ``session_file`` to control the location; the
default is ``~/.local/state/ldr_openwebui/session.json``.
"""

import os
import re
import time
import json

import requests
from bs4 import BeautifulSoup


class LDRAuthError(Exception):
    """Raised when the login handshake fails (bad credentials, etc.)."""


class LDRClient:
    """A small authenticated client for a running Local Deep Research server."""

    def __init__(self, base_url=None, username=None, password=None,
                 timeout=120, long_timeout=None, max_retries=5, backoff_factor=0.5,
                 session_file=None):
        self.base_url = (base_url or os.environ.get("LDR_BASE_URL")
                         or "http://192.168.0.3:1242").rstrip("/")
        self.username = username or os.environ.get("LDR_USERNAME")
        self.password = password or os.environ.get("LDR_PASSWORD")
        self.timeout = timeout
        self.long_timeout = long_timeout or int(
            os.environ.get("LDR_LONG_TIMEOUT", "900"))
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        if session_file is None:
            session_file = os.environ.get(
                "LDR_SESSION_FILE",
                os.path.join(
                    os.path.expanduser("~"),
                    ".local",
                    "state",
                    "ldr_openwebui",
                    "session.json",
                ),
            )
        self.session_file = session_file

        self.session = requests.Session()
        self.csrf_token = None

    # ------------------------------------------------------------------ #
    # Authentication (login once, persist, restore)
    # ------------------------------------------------------------------ #
    def ensure_authenticated(self):
        """Make sure the client is authenticated, without unnecessary logins.

        If a previously saved session exists on disk and its cookie is still
        present, it is restored and no login is performed. Otherwise the full
        login handshake runs and the session is persisted for next time.

        Returns True on success. Raises LDRAuthError on failure.
        """
        if self._restore_session():
            return True
        self.login()
        return True

    def login(self):
        """Perform the full login handshake, cache the CSRF token, and persist.

        Returns True on success. Raises LDRAuthError on failure.
        """
        if not self.username or not self.password:
            raise LDRAuthError(
                "LDR_USERNAME and LDR_PASSWORD must be set in the environment "
                f"(base_url={self.base_url})"
            )

        # 1. GET the login page and extract the CSRF token from the HTML form.
        login_page = self.session.get(
            f"{self.base_url}/auth/login", timeout=self.timeout
        )
        if login_page.status_code != 200:
            raise LDRAuthError(
                f"Failed to reach /auth/login (status {login_page.status_code})"
            )

        csrf_input = BeautifulSoup(login_page.text, "html.parser").find(
            "input", {"name": "csrf_token"}
        )
        if not csrf_input or not csrf_input.get("value"):
            raise LDRAuthError("No csrf_token found on /auth/login page")

        login_csrf = csrf_input["value"]

        # 2. POST the login form to obtain the session cookie (retry on 429).
        resp = self._login_post_with_retry(login_csrf)
        if resp.status_code not in (200, 302):
            raise LDRAuthError(
                f"Login failed (status {resp.status_code}); "
                f"check LDR_USERNAME/LDR_PASSWORD"
            )

        # 3. Fetch the API CSRF token and cache it for subsequent calls.
        self._refresh_csrf()

        # 4. Persist the session so future (fresh-process) calls can reuse it.
        self._save_session()
        return True

    def _refresh_csrf(self):
        """GET /auth/csrf-token and store the token for the X-CSRF-Token header."""
        resp = self.session.get(
            f"{self.base_url}/auth/csrf-token", timeout=self.timeout
        )
        if resp.status_code == 200:
            try:
                self.csrf_token = resp.json().get("csrf_token")
            except ValueError:
                self.csrf_token = None
        return self.csrf_token

    def _login_post_with_retry(self, login_csrf):
        """POST the login form, retrying on 429 with exponential backoff."""
        attempt = 0
        while True:
            attempt += 1
            resp = self.session.post(
                f"{self.base_url}/auth/login",
                data={
                    "username": self.username,
                    "password": self.password,
                    "csrf_token": login_csrf,
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            if resp.status_code == 429 and attempt <= self.max_retries:
                delay = min(self.backoff_factor * (2 ** (attempt - 1)), 30)
                time.sleep(delay)
                continue
            return resp

    # ------------------------------------------------------------------ #
    # Session persistence (cookies + CSRF token to a JSON file)
    # ------------------------------------------------------------------ #
    def _serialize_cookies(self):
        """Return a JSON-serializable list of the session's cookies.

        ``requests``' ``RequestsCookieJar`` has no ``save()`` method, so we
        capture the fields we need manually.
        """
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
                "expires": c.expires,
            }
            for c in self.session.cookies
        ]

    def _save_session(self):
        """Persist the session cookie(s) and CSRF token to disk for reuse."""
        try:
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            with open(self.session_file, "w") as f:
                json.dump(
                    {
                        "cookies": self._serialize_cookies(),
                        "csrf": self.csrf_token,
                    },
                    f,
                )
        except OSError:
            # Persistence is best-effort: if it fails we simply fall back to a
            # fresh login on the next call rather than crashing the tool.
            pass

    def _restore_session(self):
        """Load a saved session from disk if present.

        Returns True if a usable session was restored (cookies + csrf loaded),
        False otherwise. Never raises.
        """
        try:
            if not os.path.exists(self.session_file):
                return False
            with open(self.session_file, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False

        cookies = data.get("cookies")
        csrf = data.get("csrf")
        if not cookies or not csrf:
            return False

        try:
            for cd in cookies:
                self.session.cookies.set(
                    cd["name"],
                    cd["value"],
                    domain=cd.get("domain"),
                    path=cd.get("path", "/"),
                    secure=cd.get("secure", False),
                    expires=cd.get("expires"),
                )
            self.csrf_token = csrf
            return True
        except Exception:
            return False

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        return headers

    # ------------------------------------------------------------------ #
    # HTTP helpers with 429-aware retry
    # ------------------------------------------------------------------ #
    def _request_with_retry(self, method, path, **kwargs):
        """Send a request, retrying on 429 or transient network errors.

        Retries are applied to:
          - HTTP 429 (rate limited) responses, and
          - transient connection errors (ConnectionError, Timeout,
            RemoteDisconnected) that can occur during long-running polling.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        # Merge the auth/CSRF headers with any caller-supplied headers.
        merged = self._headers()
        merged.update(kwargs.pop("headers", {}))
        kwargs["headers"] = merged

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                # Only retry transient errors on idempotent GET requests
                # (e.g. status polling). Never retry a timed-out POST: the
                # job may already be running server-side, and retrying would
                # start a duplicate job.
                if attempt <= self.max_retries and method == "GET":
                    delay = min(self.backoff_factor * (2 ** (attempt - 1)), 30)
                    time.sleep(delay)
                    continue
                raise

            if resp.status_code == 429 and attempt <= self.max_retries:
                delay = min(self.backoff_factor * (2 ** (attempt - 1)), 30)
                time.sleep(delay)
                continue

            return resp

    def get_json(self, path):
        resp = self._request_with_retry("GET", path)
        return resp

    def post_json(self, path, body=None, timeout=None):
        """POST JSON, defaulting to the long timeout.

        Long-running server-side operations (analyze_documents, search) can
        take several minutes, so POSTs use
        ``long_timeout`` by default. Pass a positive timeout to override.
        """
        data = body if body is not None else None
        resp = self._request_with_retry(
            "POST", path, json=data,
            headers={"Content-Type": "application/json"},
            timeout=timeout if timeout is not None else self.long_timeout,
        )
        return resp

    def get(self, path):
        return self._request_with_retry("GET", path)

    def post(self, path, **kwargs):
        return self._request_with_retry("POST", path, **kwargs)
