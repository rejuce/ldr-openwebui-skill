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
    client.login()
    data = client.post_json("/api/v1/quick_summary", {"query": "..."})
"""

import os
import re
import time

import requests
from bs4 import BeautifulSoup


class LDRAuthError(Exception):
    """Raised when the login handshake fails (bad credentials, etc.)."""


class LDRClient:
    """A small authenticated client for a running Local Deep Research server."""

    def __init__(self, base_url=None, username=None, password=None,
                 timeout=120, long_timeout=None, max_retries=5, backoff_factor=0.5):
        self.base_url = (base_url or os.environ.get("LDR_BASE_URL")
                         or "http://192.168.0.3:1242").rstrip("/")
        self.username = username or os.environ.get("LDR_USERNAME")
        self.password = password or os.environ.get("LDR_PASSWORD")
        self.timeout = timeout
        self.long_timeout = long_timeout or int(
            os.environ.get("LDR_LONG_TIMEOUT", "900"))
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        self.session = requests.Session()
        self.csrf_token = None

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def login(self):
        """Perform the full login handshake and cache the CSRF token.

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
        ``long_timeout`` by default. Pass ``timeout=None`` to override (e.g. a
        short timeout for quick calls).
        """
        data = body if body is None else body
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
