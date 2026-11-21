"""
7 tool functions wrapping Local Deep Research (LDR) over HTTP.

Each tool is a thin wrapper over :class:`LDRClient` (see ``ldr_client.py``) and
returns a JSON string suitable for an LLM tool call. Every function accepts an
optional ``client`` argument so a single authenticated session can be reused
across calls.

Tools
-----
1. quick_research(query)                      -> POST /api/v1/quick_summary
2. detailed_research(query, ...)              -> POST /api/start_research + poll
3. analyze_documents(query, collection_name, max_results) -> POST /api/v1/analyze_documents
4. search(query)                              -> POST /api/start_research + poll
5. list_search_engines()                      -> GET /settings/api/available-search-engines
6. list_strategies()                          -> in-process (hardcoded)
7. get_configuration()                        -> GET /research/api/settings/current-config

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
# that single session across all tools.
_SHARED_CLIENT = None


def _get_client(client=None):
    """Return a single shared authenticated client.

    If an explicit ``client`` is passed and already authenticated, use it.
    Otherwise reuse the module-level cached client, logging in once on first
    use. This avoids tripping the login rate limiter by re-authenticating
    repeatedly.
    """
    global _SHARED_CLIENT

    if client is not None:
        if not getattr(client, "csrf_token", None):
            client.login()
        return client

    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = LDRClient()
        _SHARED_CLIENT.login()
    return _SHARED_CLIENT


def reset_cached_client():
    """Drop the cached client (e.g. between test cases)."""
    global _SHARED_CLIENT
    _SHARED_CLIENT = None


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


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
# 2. detailed_research
# --------------------------------------------------------------------------- #
def detailed_research(query, model=None, search_engines=None, iterations=None,
                      questions_per_iteration=None, strategy=None, client=None,
                      poll_interval=5, max_polls=240):
    """Start an async research job, poll to completion, and fetch the report.

    Maps to POST /api/start_research -> GET /api/research/{id}/status ->
    GET /api/report/{id}.

    Optional params (omitted -> server defaults):
        model, search_engines (list), iterations (int),
        questions_per_iteration (int), strategy (str, see list_strategies)
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
        return _dumps({"error": f"detailed_research start failed (HTTP {resp.status_code})",
                       "detail": resp.text[:500]})
    data = _ok(resp)
    research_id = data.get("research_id")
    if not research_id:
        return _dumps({"error": "no research_id returned", "response": data})

    # Poll status until completed / failed.
    for _ in range(max_polls):
        st = client.get(f"/api/research/{research_id}/status")
        if st.status_code != 200:
            time.sleep(poll_interval)
            continue
        stt = _ok(st)
        status = stt.get("status")
        if status == "completed":
            rep = client.get(f"/api/report/{research_id}")
            if rep.status_code == 200:
                return _dumps({"research_id": research_id,
                               "status": "completed",
                               **_ok(rep)})
            return _dumps({"error": f"report fetch failed (HTTP {rep.status_code})",
                           "research_id": research_id})
        if status == "failed":
            return _dumps({"research_id": research_id, "status": "failed",
                           "detail": stt})
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
