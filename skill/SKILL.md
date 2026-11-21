---
name: Local Deep Research (LDR)
description: Run research, summarize documents, and inspect configuration against a running Local Deep Research (LDR) server over HTTP. Use when asked to research a topic, search the web via the research agent, summarize a document library, list search engines or strategies, or read the current research configuration.
---

# Local Deep Research (LDR)

Run **Local Deep Research (LDR)** — an open-source, self-hosted research agent
(LlamaCPP backend) — over HTTP. LDR searches sources, synthesizes findings with
inline citations, and can summarize over a user's own document library. This
skill wraps the running server as a set of Python tool functions, each returning
a JSON string suitable for an LLM tool call.

This skill does **not** start or manage the LDR server — a server must already
be running and reachable. It also does **not** run the long `generate_report`
job, which runs for hours server-side and was intentionally dropped from the
skill.

## When to use

- The user asks to "research" or "look into" a topic (quick or deep).
- The user asks to "search" using the research agent.
- The user asks to summarize or analyze a document library / collection.
- The user asks which search engines or research strategies are available.
- The user asks for the server's current research configuration.

## Prerequisites

- A running, reachable Local Deep Research server (default `http://192.168.0.3:1242`).
- Python 3 with `requests` and `beautifulsoup4` available (install with
  `pip install -r requirements.txt` from the repo root, or
  `pip install requests beautifulsoup4`).
- Server credentials, set as environment variables (never hardcode):

```bash
export LDR_BASE_URL="http://192.168.0.3:1242"
export LDR_USERNAME="<your-username>"
export LDR_PASSWORD="<your-password>"
export LDR_LONG_TIMEOUT=900   # seconds for long POSTs
```

## How to run

The tool functions ship **inside this skill**, relative to this `SKILL.md`:
`tools/ldr_tools.py` (auth in `tools/ldr_client.py`). Because the files are
bundled with the skill, reference them by their **relative** `tools/` path and
run every command from the skill directory (the directory that contains this
`SKILL.md` and its `tools/` folder). Do not hardcode an absolute skill path.

Run the shell work through `run_command`:

```bash
python3 -c "import sys; sys.path.insert(0, 'tools'); import ldr_tools as t; print(t.<function>(...))"
```

Each function returns a JSON string; parse it to inspect the result. Every
function accepts an optional `client=` argument so one authenticated session
can be reused across calls.

## Quick reference

Tool functions in `tools/ldr_tools.py` (each returns a JSON string):

1. `quick_research(query)` — POST `/api/v1/quick_summary` → summary + findings + sources
2. `detailed_research(query, model, search_engines, iterations, questions_per_iteration, strategy)` — POST `/api/start_research`, poll `GET /api/research/{id}/status`, fetch `GET /api/report/{id}`
3. `analyze_documents(query, collection_name, max_results)` — POST `/api/v1/analyze_documents` (long-running)
4. `search(query)` — research pipeline, single iteration, `focused-iteration` → raw findings
5. `list_search_engines()` — GET `/settings/api/available-search-engines` → engines + options + favorites
6. `list_strategies()` — in-process (no HTTP endpoint) → 5 strategies
7. `get_configuration()` — GET `/research/api/settings/current-config` → server config

Auth client in `tools/ldr_client.py` (`LDRClient`): session-cookie + CSRF-token
handshake with 429-aware retry.

Dependencies: `requests`, `beautifulsoup4` (`requirements.txt`).

## Procedure

1. Confirm the server is reachable (see Verification). If credentials are
   required, set the environment variables in Prerequisites first.
2. For a fast answer, run `quick_research(query)`.
3. For a thorough job, run `detailed_research(query, iterations=..., strategy=...)`; it polls to completion and returns the report.
4. For raw search findings, run `search(query)` (single iteration).
5. To summarize a document library, run `analyze_documents(query, collection_name=..., max_results=...)` — allow many minutes.
6. To discover options, run `list_search_engines()` and `list_strategies()`.
7. To inspect the server's live config, run `get_configuration()`.

## Pitfalls

- **Login rate limiter:** the login endpoint 429s if called too quickly. The
  tools authenticate **once** and reuse a single session, so normal use is
  fine — never add per-call logins.
- **Server overload:** repeated long-running POSTs (`analyze_documents`,
  `detailed_research`, `search`) can overload the server, causing HTTP 500 or
  `still_running`. Give the server recovery time between heavy calls.
- **Long timeouts:** long POSTs use `LDR_LONG_TIMEOUT` (default 900s).
- **Self-contained paths:** run the tools from the skill directory using the
  relative `tools/` path. Never hardcode an absolute skill path.
- **No `generate_report`:** that job runs hours server-side and was intentionally
  dropped from the skill.

## Verification

Run the in-process strategy list (no server load required) and confirm five
strategies are returned:

```bash
python3 -c "import sys; sys.path.insert(0, 'tools'); import ldr_tools as t; print(t.list_strategies())"
```

A JSON string with a `strategies` list of five names — `source-based`,
`focused-iteration`, `focused-iteration-standard`, `topic-organization`,
`langgraph-agent` — proves the skill and its bundled tool files are correctly
installed.
