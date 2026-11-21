---
name: Local Deep Research (LDR)
description: Run research, summarize documents, and inspect configuration against a running Local Deep Research (LDR) server. Use when asked to research a topic, search the web via the research agent, summarize a document library, list search engines or strategies, or read the current research configuration.
---

# Local Deep Research (LDR)

Run **Local Deep Research (LDR)** — an open-source, self-hosted research agent
(LlamaCPP backend) — over HTTP. LDR searches sources, synthesizes findings with
inline citations, and can summarize over a user's own document library. This
skill wraps the running server as a set of clean Python tool functions that
return JSON strings, suitable for an LLM tool call.

## When to use

- The user asks to "research" or "look into" a topic (quick or deep).
- The user asks to "search" using the research agent.
- The user asks to summarize or analyze a document library/collection.
- The user asks what search engines or research strategies are available.
- The user asks for the server's current research configuration.

## How it works

The tools live in `tools/ldr_tools.py` and depend on `tools/ldr_client.py`
(auth handshake + retry). They return a **JSON string** for each call.

The canonical tools live at `/workspace/repos/ldr-openwebui-skill/tools/`.
Invoke them from Python:

```bash
cd /workspace/repos/ldr-openwebui-skill/tools
python3 -c "import ldr_tools as t; print(t.<function>(...))"
```

Or, from a Python script that adds the tools directory to `sys.path`:

```python
import sys
sys.path.insert(0, "/workspace/repos/ldr-openwebui-skill/tools")
import ldr_tools as t
```

### Configuration (environment variables)

Set these before running (never hardcode credentials):

```bash
export LDR_BASE_URL="http://192.168.0.3:1242"   # default
export LDR_USERNAME="<your-username>"
export LDR_PASSWORD="<your-password>"
export LDR_LONG_TIMEOUT=900                       # seconds for long POSTs
```

## The 7 tools

### 1. quick_research — fast single-iteration Q&A

```bash
python3 -c "import ldr_tools as t; print(t.quick_research('What is the capital of France?'))"
```

Maps to `POST /api/v1/quick_summary`. Returns `{research_id, summary, findings, sources}`.

### 2. detailed_research — full async research job

```bash
python3 -c "import ldr_tools as t; print(t.detailed_research('History of coffee'))"
```

Maps to `POST /api/start_research`, then polls `GET /api/research/{id}/status`
until `completed`, then fetches `GET /api/report/{id}`.

Optional parameters (all omitted → server defaults):
- `model`, `search_engines` (list), `iterations` (int),
  `questions_per_iteration` (int), `strategy` (str, see #6)

Returns `{research_id, status, ...report fields}`.

### 3. analyze_documents — summarize a document library

```bash
python3 -c "import ldr_tools as t; print(t.analyze_documents('Summarize research on coffee', collection_name='default', max_results=3))"
```

Maps to `POST /api/v1/analyze_documents`. **Long-running** (may take many
minutes). Returns `{success, ...}`. If it returns a timeout or HTTP 500, the
server was busy — wait and retry.

### 4. search — raw search findings

```bash
python3 -c "import ldr_tools as t; print(t.search('history of coffee'))"
```

Uses the research pipeline with a minimal config (single iteration,
`focused-iteration` strategy) to surface search findings quickly.

### 5. list_search_engines — available engines + favorites

```bash
python3 -c "import ldr_tools as t; print(t.list_search_engines())"
```

Maps to `GET /settings/api/available-search-engines`.
Returns `{engines, engine_options, favorites}`.

### 6. list_strategies — research strategies (in-process)

```bash
python3 -c "import ldr_tools as t; print(t.list_strategies())"
```

No HTTP endpoint — returns the 5 hardcoded strategies:
`source-based`, `focused-iteration`, `focused-iteration-standard`,
`topic-organization`, `langgraph-agent`.

### 7. get_configuration — current server config

```bash
python3 -c "import ldr_tools as t; print(t.get_configuration())"
```

Maps to `GET /research/api/settings/current-config`.
Returns `{success, config: {...}}` (provider, model, search_tool, iterations,
questions_per_iteration, search_strategy).

## Server constraints

- **Login rate limiter:** the login endpoint 429s if called too quickly. The
  tools authenticate **once** and reuse a single session, so normal use is fine.
- **Server overload:** repeated long-running POSTs (`analyze_documents`,
  `detailed_research`, `search`) can overload the server, causing HTTP 500 or
  `still_running`. Give the server recovery time between heavy calls.
- **Long timeouts:** long POSTs use `LDR_LONG_TIMEOUT` (default 900s).

## Files

```
ldr-openwebui-skill/
├── SKILL.md               ← this file
├── README.md              ← project overview
├── requirements.txt
├── .gitignore
├── tools/
│   ├── ldr_client.py      ← auth handshake + retry/backoff
│   └── ldr_tools.py       ← 7 tool functions
├── agent/                 ← architecture.md, progress.md
└── tests/                 ← pytest suite
```

## Dependencies

- `requests`, `beautifulsoup4` (Python)
- A running Local Deep Research server (default `http://192.168.0.3:1242`)
