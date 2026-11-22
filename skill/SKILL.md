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

> ⚠️ **Never read, edit, or execute from the development repository.** The
> `tools/` folder in the development repo where this skill was authored is this
> skill's **source**, not what you run. Reading from, editing, or executing
> against it would corrupt the source. The files you run are the **packaged**
> skill, served to you by `read_skill_file`.

The tool functions ship **inside this skill** as `tools/ldr_client.py` (auth)
and `tools/ldr_tools.py` (the 7 wrappers). In agent mode these are served to you
by `read_skill_file`; they are **not** directly readable by `run_command`, and
you are always the relay between them. To execute them safely:

1. **Read** the tool files via `read_skill_file` (or `view_skill`):
   `tools/ldr_client.py` and `tools/ldr_tools.py`.
2. **Write** them to a **temporary** working directory — never into the
   development repo. Create it fresh each run:
   ```bash
   WORKDIR="$(mktemp -d)"
   ```
3. **Execute** from that temporary directory:
   ```bash
   cd "$WORKDIR"
   python3 -c "import sys; sys.path.insert(0, '.'); import ldr_tools as t; print(t.<function>(...))"
   ```
4. **Clean up** afterwards:
   ```bash
   rm -rf "$WORKDIR"
   ```

Because `ldr_tools.py` imports `ldr_client.py` at the top, both files must be
present in the temp directory together before executing — hence the
write-then-run workflow (a single-file stdin pipe cannot satisfy the import).

Each function returns a JSON string; parse it to inspect the result. Every
function accepts an optional `client=` argument so one authenticated session
can be reused across calls.

## Quick reference

Tool functions in `tools/ldr_tools.py` (each returns a JSON string):
1. `quick_research(query)` — POST `/api/v1/quick_summary` → summary + findings + sources
2. `detailed_research(query, model, search_engines, iterations, questions_per_iteration, strategy)` — compose the async trio below (start -> poll -> fetch)
2b. **Async research trio** (non-blocking; use directly for long jobs):
    - `start_research(query, ...)` — POST `/api/start_research` -> `{status:"success", research_id}` (returns immediately; job runs server-side)
    - `research_status(research_id)` — GET `/api/research/{id}/status` -> `{status: in_progress|completed|failed, ...}`
    - `fetch_report(research_id)` — GET `/api/report/{id}` -> `{content, summary, sources, findings, ...}`
    - `list_researches()` — GET `/library/api/research-list` -> `{research:[{id,title,query}]}`
    - `queue_status()` — GET `/api/queue/status` -> `{status:"success", queue:[], total:N}`
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
3. For a thorough job, run `detailed_research(query, iterations=..., strategy=...)`; it polls to completion and returns the report. For very long jobs you can drive the async trio directly — `start_research()` (fire-and-forget), then poll `research_status()` and finally `fetch_report()`.
4. To check what has already run, run `list_researches()`; to check server load, run `queue_status()`.
5. For raw search findings, run `search(query)` (single iteration).
6. To summarize a document library, run `analyze_documents(query, collection_name=..., max_results=...)` — allow many minutes.
7. To discover options, run `list_search_engines()` and `list_strategies()`.
8. To inspect the server's live config, run `get_configuration()`.

## Pitfalls

- **Never execute from or edit the development repo:** the repo's `tools/` folder
  is this skill's source. Read the packaged files via `read_skill_file`, write
  them to a temporary directory, execute from there, and clean up.
- **Login rate limiter:** the login endpoint 429s if called too quickly. The
  tools authenticate **once** and reuse a single session, so normal use is
  fine — never add per-call logins.
- **Server overload:** repeated long-running POSTs (`analyze_documents`,
  `detailed_research`, `search`) can overload the server, causing HTTP 500 or
  `still_running`. Give the server recovery time between heavy calls.
- **Long timeouts:** long POSTs use `LDR_LONG_TIMEOUT` (default 900s).
- **Empty report is NOT success:** the tiny server model (Ornith-1.5-35B-A3B-EXL3-4bpw) can return `status == "completed"` with **empty** `findings`/`content`/`summary`. `detailed_research` attaches a `validated` flag (and `validation_note`) — check it before treating a result as a success.
- **`progress` is always null:** `research_status()` returns `progress: null`; there is no numeric progress percentage. Do not rely on it — poll `status` only.
- **No `generate_report`:** that job runs hours server-side and was intentionally
  dropped from the skill.

## Verification

Run the in-process strategy list (no server load required) and confirm five
strategies are returned. Execute in a **temporary** directory (see "How to run"):

```bash
cd "$(mktemp -d)" && python3 -c "import sys; sys.path.insert(0, '.'); import ldr_tools as t; print(t.list_strategies())"
```

A JSON string with a `strategies` list of five names — `source-based`,
`focused-iteration`, `focused-iteration-standard`, `topic-organization`,
`langgraph-agent` — proves the skill and its bundled tool files are correctly
installed.
