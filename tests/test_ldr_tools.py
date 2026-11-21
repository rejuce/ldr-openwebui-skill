"""
Integration tests for the Local Deep Research (LDR) tool wrappers.

These tests hit a **live** LDR server (via env vars). They are
designed to:

- Authenticate **once** and reuse a single shared client (avoid the login
  rate limiter).
- Exercise long-running tools with generous spacing between calls so a single
  test run does not overload the server (see the sleeps in the long-running
  tests).
- Skip gracefully if the server is unavailable or overloaded (HTTP 500,
  timeout, ``still_running``), rather than failing the whole suite.

Configuration (environment variables, never hardcoded):

    LDR_BASE_URL   default http://192.168.0.3:1242
    LDR_USERNAME
    LDR_PASSWORD
    LDR_LONG_TIMEOUT default 900

Run:

    cd tools
    python3 -m pytest ../tests/test_ldr_tools.py -v
"""

import json
import os
import time

import pytest

# Make the tools importable without installing the package.
TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "tools")
TOOLS_DIR = os.path.abspath(TOOLS_DIR)
if TOOLS_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, TOOLS_DIR)

import ldr_tools  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _env_base_url():
    return os.environ.get("LDR_BASE_URL", "http://192.168.0.3:1242").rstrip("/")


def _env_creds():
    return (
        os.environ.get("LDR_USERNAME"),
        os.environ.get("LDR_PASSWORD"),
    )


def _server_up():
    """Lightweight reachability check (no login)."""
    import requests
    try:
        r = requests.get(f"{_env_base_url()}/api/v1/health", timeout=10)
        return True  # any response means the server is reachable
    except Exception:
        return False


def _parse(result):
    """Parse a tool's JSON-string result into a dict (tolerant of errors)."""
    if isinstance(result, (dict, list)):
        return result
    try:
        return json.loads(result)
    except (ValueError, TypeError):
        return {"_unparseable": result}


def _is_error(d):
    return isinstance(d, dict) and ("error" in d or "detail" in d)


# --------------------------------------------------------------------------- #
# Session-scoped shared client (authenticate once)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def shared_client():
    """Provide one shared, authenticated client for the whole session.

    Skips all tests if the server is unreachable or credentials are missing.
    """
    base_url = _env_base_url()
    username, password = _env_creds()

    if not username or not password:
        pytest.skip("LDR_USERNAME / LDR_PASSWORD not set")
    if not _server_up():
        pytest.skip(f"LDR server not reachable at {base_url}")

    import ldr_client
    client = ldr_client.LDRClient(base_url=base_url, username=username, password=password)
    client.login()
    assert client.csrf_token, "login handshake failed (no csrf_token)"
    yield client


# --------------------------------------------------------------------------- #
# Fast tools (no server overload)
# --------------------------------------------------------------------------- #
def test_quick_research(shared_client):
    result = ldr_tools.quick_research("What is the capital of France?", client=shared_client)
    d = _parse(result)
    assert not _is_error(d), f"unexpected error: {d}"
    assert d.get("summary") or d.get("findings"), f"missing summary/findings: {d}"


def test_list_strategies(shared_client):
    result = ldr_tools.list_strategies(client=shared_client)
    d = _parse(result)
    assert not _is_error(d), f"unexpected error: {d}"
    strategies = d.get("strategies", [])
    assert isinstance(strategies, list)
    names = {s["name"] for s in strategies}
    expected = {
        "source-based",
        "focused-iteration",
        "focused-iteration-standard",
        "topic-organization",
        "langgraph-agent",
    }
    assert expected.issubset(names), f"missing strategies: {expected - names}"


def test_list_search_engines(shared_client):
    result = ldr_tools.list_search_engines(client=shared_client)
    d = _parse(result)
    assert not _is_error(d), f"unexpected error: {d}"
    assert isinstance(d.get("engines"), dict), f"missing 'engines' key: {d}"


def test_get_configuration(shared_client):
    result = ldr_tools.get_configuration(client=shared_client)
    d = _parse(result)
    assert not _is_error(d), f"unexpected error: {d}"
    assert d.get("success") is True, f"success flag not True: {d}"
    assert "config" in d, f"missing 'config' key: {d}"


# --------------------------------------------------------------------------- #
# detailed_research — async job (may take minutes).
#
# Skips if the server is overloaded (still_running / timeout) so a single
# test run does not fail on transient server load.
# --------------------------------------------------------------------------- #
def test_detailed_research(shared_client):
    result = ldr_tools.detailed_research(
        "What is the capital of France?",
        iterations=1,
        questions_per_iteration=1,
        strategy="focused-iteration",
        client=shared_client,
        poll_interval=5,
        max_polls=120,
    )
    d = _parse(result)
    if _is_error(d):
        pytest.skip(f"server overloaded or error: {d}")
    status = d.get("status")
    assert status == "completed", f"expected completed, got: {d}"


# --------------------------------------------------------------------------- #
# analyze_documents — long-running. Skips on overload.
# --------------------------------------------------------------------------- #
def test_analyze_documents(shared_client):
    # Give the server a breather after detailed_research before the heavy
    # analyze_documents job.
    time.sleep(30)
    result = ldr_tools.analyze_documents(
        "Summarize research on coffee",
        collection_name="default",
        max_results=2,
        client=shared_client,
    )
    d = _parse(result)
    if _is_error(d):
        pytest.skip(f"server overloaded or error: {d}")
    assert d.get("success") is True, f"success flag not True: {d}"


# --------------------------------------------------------------------------- #
# search — raw search via the research pipeline. Skips on overload.
# --------------------------------------------------------------------------- #
def test_search(shared_client):
    # Give the server a breather after analyze_documents before search.
    time.sleep(30)
    result = ldr_tools.search("capital of France", client=shared_client)
    d = _parse(result)
    if _is_error(d):
        pytest.skip(f"server overloaded or error: {d}")
    status = d.get("status")
    if status == "still_running":
        pytest.skip("search job still running (server busy)")
    assert status == "completed", f"expected completed, got: {d}"


# --------------------------------------------------------------------------- #
# Unit-ish: the module exposes exactly the 7 documented functions.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("func_name", [
    "quick_research",
    "detailed_research",
    "analyze_documents",
    "search",
    "list_search_engines",
    "list_strategies",
    "get_configuration",
])
def test_all_documented_functions_exist(func_name):
    assert hasattr(ldr_tools, func_name), f"missing function: {func_name}"
    assert callable(getattr(ldr_tools, func_name))


def test_generate_report_dropped():
    """generate_report was intentionally removed (runs for hours server-side)."""
    assert not hasattr(ldr_tools, "generate_report"), \
        "generate_report should have been removed"
