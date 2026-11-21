# Architecture — Local Deep Research (LDR) OpenWebUI Skill

## Purpose

A Python skill package that wraps a running **Local Deep Research (LDR)** server
over HTTP, exposing it to an LLM / OpenWebUI as a set of clean tool functions.
The LDR server is an open-source research agent (LlamaCPP backend) that runs
searches, synthesizes findings with inline citations, and can summarize over a
user's own document library.

## Top-level layout

```
ldr-openwebui-skill/
├── SKILL.md               ← OpenWebUI / agent skill instructions (frontmatter)
├── README.md              ← human-readable project overview
├── requirements.txt       ← Python dependencies
├── .gitignore
├── tools/
│   ├── ldr_client.py      ← LDRClient: auth handshake + retry/backoff
│   └── ldr_tools.py       ← 7 tool functions (thin JSON wrappers)
├── agent/
│   ├── architecture.md    ← this file
│   └── progress.md        ← living log of steps taken
└── tests/
    └── test_ldr_tools.py  ← pytest suite (shared client, spaced calls)
```

## Authentication flow (LDRClient)

LDR does **not** use an API key. It authenticates with a session-cookie /
CSRF-token handshake:

1. `GET  /auth/login`      → parse `<input name="csrf_token">` from the HTML form
2. `POST /auth/login`      → form-encoded (username, password, csrf_token)
                             → sets the `session` cookie
3. `GET  /auth/csrf-token` → `{"csrf_token": "..."}` → sent as the
                             `X-CSRF-Token` header on every API call

Configuration comes from environment variables (never hardcoded):

```
LDR_BASE_URL   default http://192.168.0.3:1242
LDR_USERNAME
LDR_PASSWORD
LDR_LONG_TIMEOUT default 900 (seconds; long-running POSTs)
```

## Retry / backoff strategy

The LDR login endpoint has a tight "too many attempts" rate limiter (429).
Two mitigations:

- **Single shared client.** The module-level `_SHARED_CLIENT` in `ldr_tools.py`
  authenticates **once** and is reused across all tool calls. We never log in
  per-call.
- **429-aware retry with exponential backoff.** `LDRClient._request_with_retry`
  retries transient errors on **GET** requests only. POSTs are never retried on
  timeout — the job may already be running server-side, and a retry would start
  a duplicate job.

## Tool functions (7)

| # | Function | Endpoint | Notes |
|---|----------|----------|-------|
| 1 | `quick_research(query)` | POST `/api/v1/quick_summary` | Fast single-iteration Q&A |
| 2 | `detailed_research(query, ...)` | POST `/api/start_research` + poll | Async job, poll to completion |
| 3 | `analyze_documents(query, collection_name, max_results)` | POST `/api/v1/analyze_documents` | Summarize over a doc library |
| 4 | `search(query)` | research pipeline | Raw search via `detailed_research` (1 iteration) |
| 5 | `list_search_engines()` | GET `/settings/api/available-search-engines` | Engines + options + favorites |
| 6 | `list_strategies()` | in-process (hardcoded) | 5 strategies, no HTTP endpoint |
| 7 | `get_configuration()` | GET `/research/api/settings/current-config` | Server's current config |

All functions return a **JSON string** (via `_dumps`) suitable for an LLM tool
call. Each accepts an optional `client=` argument so a single authenticated
session can be reused.

## Endpoint contract (verified against `/openapi.json`)

- `POST /api/v1/quick_summary` → `{research_id, summary, findings, sources}`
- `POST /api/start_research` → `{research_id}` → poll `GET /api/research/{id}/status`
  → `GET /api/report/{id}` on completion
- `POST /api/v1/analyze_documents` → `{success, ...}` (long-running, may 500 under load)
- `GET /settings/api/available-search-engines` → `{engines, engine_options, favorites}`
- `GET /research/api/settings/current-config` → `{success, config: {...}}`

## Known server constraints

1. **Login 429 limiter** — wait ≥180s between login attempts.
2. **Server overload** — repeated long-running POSTs overload the server,
   causing `analyze_documents` to return HTTP 500 and `search`/`detailed_research`
   to return `still_running`. Give the server recovery time between heavy calls.
3. **Long-running POSTs** — `analyze_documents` can take many minutes. POSTs use
   `long_timeout` (default 900s) via `LDR_LONG_TIMEOUT`.

## Dropped: `generate_report`

`POST /api/v1/generate_report` was dropped by the Lead Engineer. It runs for
hours server-side (not minutes), making it awkward from the OpenWebUI UI. The
endpoint still exists on the server; only the tool wrapper was removed.
