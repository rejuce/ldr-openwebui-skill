"""
7 tool functions (plus an async research trio) wrapping Local Deep Research (LDR) over HTTP.

Each tool is a thin wrapper over :class:`LDRClient` (see ``ldr_client.py``) and
returns a JSON string suitable for an LLM tool call. Every function accepts an
optional ``client`` argument so a single authenticated session can be reused
across calls.

Tools
-----
1. quick_research(query)                      -> POST /api/v1/quick_summary
2. detailed_research(query, ...)              -> compose the async trio (below)
3. analyze_documents(query, collection_name, max_results) -> POST /api/v1/analyze_documents
4. search(query)                              -> detailed_research (single iteration)
5. list_search_engines()                      -> GET /settings/api/available-search-engines
6. list_strategies()                          -> in-process (hardcoded)
7. get_configuration()                        -> GET /research/api/settings/current-config

Async research trio (non-blocking start + poll + fetch):
8. start_research(query, ...)                 -> POST /api/start_research -> {research_id}
9. research_status(research_id)               -> GET /api/research/{id}/status
10. fetch_report(research_id)                 -> GET /api/report/{id}
11. list_researches()                         -> GET /library/api/research-list
12. queue_status()                            -> GET /api/queue/status

Configuration (environment variables):
    LDR_BASE_URL   default http://192.168.0.3:1242
    LDR_USERNAME
    LDR_PASSWORD
"""

import json
import time

import requests
from ldr_client import LDRClient


# --- Strategies: in-process, no HTTP endpoint -------------------------------
# Mirrors get_available_strategies() / AVAILABLE_STRATEGIES in constants.py.
STRATEGIES = [
    {
        "name": "source-based",
        "label": "Source-Based (Best for small <16,000 context window)",
        "description": (
            "Comprehensive research with inline citations. Focuses on finding "
            "and extracting information from authoritative sources."
        ),
    },
    {
        "name": "focused-iteration",
        "label": "Focused Iteration - Quick (Minimal text output)",
        "description": (
            "Fast & precise Q&A with iterative search. Good for complex "
            "queries requiring specific answers."
        ),
    },
    {
        "name": "focused-iteration-standard",
        "label": "Focused Iteration - Comprehensive (Needs >16,000 context window)",
        "description": (
            "Detailed long-form output with citations. Uses standard citation "
            "handler for comprehensive answers."
        ),
    },
    {
        "name": "topic-organization",
        "label": "Topic Organization (Clusters by topic)",
        "description": (
            "Clusters sources into topics with lead texts. Organizes research "
            "by themes for structured output."
        ),
    },
    {
        "name": "langgraph-agent",
        "label": "LangGraph Agent (Autonomous agentic research)",
        "description": (
            "Agentic research where the LLM autonomously decides what to search, "
            "which engines to use, and when to synthesize. Supports all search "
            "engines as tools."
        ),
    },
]


# Module-level cached authenticated client.
#
# The LDR login endpoint has a tight "too many attempts" rate limiter, so we
# must NOT log in on every tool call. Instead we authenticate once and reuse
# that single session across all tools. Session persistence (see ldr_client)
# lets that single session survive the fresh-process boundary between LLM tool
# calls, so we never have to re-login.
_SHARED_CLIENT = None


def _get_client(client=None):
    """Return a single shared authenticated client.

    If an explicit ``client`` is passed and already authenticated, use it.
    Otherwise reuse the module-level cached client, authenticating once on first
    use. Authentication goes through ``ensure_authenticated()`` so a persisted
    session on disk is restored instead of re-login (which trips the 429 rate
    limiter).
    """
    global _SHARED_CLIENT

    if client is not None:
        if not getattr(client, "csrf_token", None):
            client.ensure_authenticated()
        return client

    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = LDRClient()
        _SHARED_CLIENT.ensure_authenticated()
    return _SHARED_CLIENT


def reset_cached_client():
    """Drop the cached client (e.g. between test cases)."""
    global _SHARED_CLIENT
    _SHARED_CLIENT = None


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _loads(s):
    """Parse a tool's JSON-string result into an object (tolerant of errors)."""
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


def _ok(resp):
    """Safely parse a 200 response's JSON body into a dict.

    ``requests`` raises ``ValueError`` (a 200 response with an empty or
    malformed body) which is *not* a ``requests.exceptions.RequestException``.
    Rather than crash the tool, return an error dict so the caller always
    gets a valid JSON string back.
    """
    try:
        return resp.json()
    except ValueError:
        return {"error": "server returned a malformed JSON body",
                "detail": resp.text[:500]}


def _is_error(d):
    """True if a parsed tool result looks like an error dict."""
    return isinstance(d, dict) and ("error" in d or "detail" in d)


def _validate_report(data):
    """Validate a research report dict.

    Returns ``(is_valid, message)``. A report is considered invalid when it is
    empty — the tiny server model (Ornith-1.5-35B-A3B-EXL3-4bpw) can return
    status ``"completed"`` with empty ``findings``/``content``/``summary``,
    masking a failed synthesis as a success. Callers should check the
    ``validated`` flag rather than trusting ``status == "completed"`` alone.
    """
    if not isinstance(data, dict):
        return False, "report is not a JSON object"
    findings = data.get("findings") or []
    content = (data.get("content") or "").strip()
    summary = (data.get("summary") or "").strip()
    sources = data.get("sources") or []
    if not findings and not content and not summary:
        return False, (
            "report is empty (no findings, content, or summary); the server "
            "model may not have synthesized"
        )
    return True, "ok"


# --------------------------------------------------------------------------- #
# 1. quick_research
# --------------------------------------------------------------------------- #
def quick_research(query, client=None):
    """Fast single-iteration Q&A. Returns summary + findings + sources.

    Maps to POST /api/v1/quick_summary.
    """
    client = _get_client(client)
    resp = client.post_json("/api/v1/quick_summary", {"query": query})
    if resp.status_code != 200:
        return _dumps({"error": f"quick_research failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))


# --------------------------------------------------------------------------- #
# Async research trio (non-blocking start + poll + fetch)
# --------------------------------------------------------------------------- #
def start_research(query, model=None, search_engines=None, iterations=None,
                   questions_per_iteration=None, strategy=None, client=None):
    """Start an async research job and return immediately with a research_id.

    Maps to POST /api/start_research. The server returns
    ``{"status": "success", "research_id": "..."}`` almost immediately; the job
    then runs server-side. Poll :func:`research_status` and finally
    :func:`fetch_report`.
    """
    client = _get_client(client)
    body = {"query": query}
    if model is not None:
        body["model"] = model
    if search_engines is not None:
        body["search_engines"] = search_engines
    if iterations is not None:
        body["iterations"] = iterations
    if questions_per_iteration is not None:
        body["questions_per_iteration"] = questions_per_iteration
    if strategy is not None:
        body["strategy"] = strategy

    resp = client.post_json("/api/start_research", body)
    if resp.status_code != 200:
        return _dumps({"error": f"start_research failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    data = _ok(resp)
    research_id = data.get("research_id")
    if not research_id:
        return _dumps({"error": "no research_id returned", "response": data})
    return _dumps({"status": "success", "research_id": research_id,
                   "note": "job started server-side; poll research_status(), then fetch_report()"})


def research_status(research_id, client=None):
    """Check the status of an async research job.

    Maps to GET /api/research/{id}/status. Returns
    ``{"status": "in_progress" | "completed" | "failed", ...}``.

    NOTE: the ``progress`` field is always ``null`` on this server — no numeric
    progress percentage is available. Do not rely on it.
    """
    client = _get_client(client)
    resp = client.get(f"/api/research/{research_id}/status")
    if resp.status_code != 200:
        return _dumps({"error": f"research_status failed (HTTP {resp.status_code})",
                       "research_id": research_id, "detail": resp.text[:500]})
    return _dumps(_ok(resp))


def fetch_report(research_id, client=None):
    """Fetch the finished research report.

    Maps to GET /api/report/{id}. Returns
    ``{"content", "summary", "sources", "findings", ...}``.

    IMPORTANT: with the tiny server model the report may come back with
    ``status == "completed"`` yet EMPTY findings/summary — the model cannot
    synthesize. Use :func:`_validate_report` (see :func:`detailed_research`)
    before treating the result as a success.
    """
    client = _get_client(client)
    resp = client.get(f"/api/report/{research_id}")
    if resp.status_code != 200:
        return _dumps({"error": f"fetch_report failed (HTTP {resp.status_code})",
                       "research_id": research_id, "detail": resp.text[:500]})
    return _dumps(_ok(resp))


def list_researches(client=None):
    """List past research jobs.

    Maps to GET /library/api/research-list ->
    ``{"research": [{id, title, query}, ...]}``.
    """
    client = _get_client(client)
    resp = client.get("/library/api/research-list")
    if resp.status_code != 200:
        return _dumps({"error": f"list_researches failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))


def queue_status(client=None):
    """Check the server research queue.

    Maps to GET /api/queue/status ->
    ``{"status": "success", "queue": [...], "total": N}``.
    """
    client = _get_client(client)
    resp = client.get("/api/queue/status")
    if resp.status_code != 200:
        return _dumps({"error": f"queue_status failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))


# --------------------------------------------------------------------------- #
# 2. detailed_research (composes the trio)
# --------------------------------------------------------------------------- #
def detailed_research(query, model=None, search_engines=None, iterations=None,
                      questions_per_iteration=None, strategy=None, client=None,
                      poll_interval=5, max_polls=240):
    """Start an async research job, poll to completion, and fetch the report.

    High-level convenience wrapper over the non-blocking trio:
    start_research -> research_status (poll) -> fetch_report.

    Optional params (omitted -> server defaults):
        model, search_engines (list), iterations (int),
        questions_per_iteration (int), strategy (str, see list_strategies)

    Returns the report with a ``validated`` flag. A "completed" status with an
    empty report is possible when the tiny server model cannot synthesize —
    check ``validated`` rather than trusting ``status == "completed"`` alone.
    """
    client = _get_client(client)

    started = _loads(start_research(
        query, model=model, search_engines=search_engines, iterations=iterations,
        questions_per_iteration=questions_per_iteration, strategy=strategy,
        client=client))
    if _is_error(started) or not started.get("research_id"):
        return _dumps(started)
    research_id = started["research_id"]

    for _ in range(max_polls):
        st = _loads(research_status(research_id, client=client))
        if _is_error(st):
            return _dumps(st)
        status = st.get("status")
        if status == "completed":
            rep = _loads(fetch_report(research_id, client=client))
            if _is_error(rep):
                return _dumps(rep)
            valid, note = _validate_report(rep)
            rep["validated"] = valid
            rep["validation_note"] = note
            return _dumps({"research_id": research_id, "status": "completed",
                           **rep})
        if status == "failed":
            return _dumps({"research_id": research_id, "status": "failed",
                           "detail": st})
        time.sleep(poll_interval)

    return _dumps({"research_id": research_id, "status": "still_running",
                   "note": "timed out polling"})


# --------------------------------------------------------------------------- #
# 3. analyze_documents
# --------------------------------------------------------------------------- #
def analyze_documents(query, collection_name="default", max_results=10, client=None):
    """Summarize over a document collection.

    Maps to POST /api/v1/analyze_documents.
    """
    client = _get_client(client)
    body = {"query": query, "collection_name": collection_name,
            "max_results": max_results}
    try:
        resp = client.post_json("/api/v1/analyze_documents", body)
    except requests.exceptions.ReadTimeout:
        return _dumps({"error": "analyze_documents timed out after the server-side "
                                "processing limit; the job may still be running. "
                                "Retry analyze_documents shortly."})
    except requests.exceptions.RequestException as e:
        return _dumps({"error": f"analyze_documents request failed: {e}"})
    if resp.status_code != 200:
        return _dumps({"error": f"analyze_documents failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))


# --------------------------------------------------------------------------- #
# 4. search  (raw search results via the research pipeline)
# --------------------------------------------------------------------------- #
def search(query, client=None):
    """Run a minimal fast research job and return its findings.

    There is no dedicated raw-search HTTP endpoint, so this uses the research
    pipeline with a minimal config (single iteration, focused-iteration
    strategy) to surface the search findings.
    """
    return detailed_research(
        query,
        search_engines=None,
        iterations=1,
        questions_per_iteration=1,
        strategy="focused-iteration",
        client=client,
        poll_interval=5,
        max_polls=120,
    )


# --------------------------------------------------------------------------- #
# 5. list_search_engines
# --------------------------------------------------------------------------- #
def list_search_engines(client=None):
    """List available search engines, options, and favorites.

    Maps to GET /settings/api/available-search-engines.
    """
    client = _get_client(client)
    resp = client.get("/settings/api/available-search-engines")
    if resp.status_code != 200:
        return _dumps({"error": f"list_search_engines failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))


# --------------------------------------------------------------------------- #
# 6. list_strategies  (in-process, no HTTP endpoint)
# --------------------------------------------------------------------------- #
def list_strategies(client=None):
    """List available research strategies (in-process; no HTTP endpoint).

    Mirrors get_available_strategies() / AVAILABLE_STRATEGIES in constants.py.
    """
    return _dumps({"strategies": STRATEGIES})


# --------------------------------------------------------------------------- #
# 7. get_configuration
# --------------------------------------------------------------------------- #
def get_configuration(client=None):
    """Return the server's current research configuration.

    Maps to GET /research/api/settings/current-config.
    """
    client = _get_client(client)
    resp = client.get("/research/api/settings/current-config")
    if resp.status_code != 200:
        return _dumps({"error": f"get_configuration failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    return _dumps(_ok(resp))
