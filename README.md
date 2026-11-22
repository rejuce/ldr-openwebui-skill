# Local Deep Research (LDR) — OpenWebUI Skill

A Python skill package that wraps a running **Local Deep Research (LDR)** server
over HTTP and exposes it to an LLM / OpenWebUI as a set of clean tool functions.

LDR is an open-source, self-hosted research agent (LlamaCPP backend). It searches
sources, synthesizes findings with inline citations, and can summarize over a
user's own document library.

## The tools (with an async research trio)

| # | Function | Endpoint | Returns |
|---|----------|----------|---------|
| 1 | `quick_research(query)` | POST `/api/v1/quick_summary` | summary + findings + sources |
| 2 | `detailed_research(query, ...)` | compose the async trio below | async research report |
| 2b | `start_research(query, ...)` | POST `/api/start_research` | `{status:"success", research_id}` (non-blocking) |
| 2c | `research_status(research_id)` | GET `/api/research/{id}/status` | `{status: in_progress|completed|failed, progress:null}` |
| 2d | `fetch_report(research_id)` | GET `/api/report/{id}` | `{content, summary, sources, findings}` |
| 3 | `list_researches()` | GET `/library/api/research-list` | `{research:[{id,title,query}]}` |
| 4 | `queue_status()` | GET `/api/queue/status` | `{status:"success", queue:[], total:N}` |
| 5 | `analyze_documents(query, collection_name, max_results)` | POST `/api/v1/analyze_documents` | summarize a doc library |
| 6 | `search(query)` | research pipeline | raw search findings |
| 7 | `list_search_engines()` | GET `/settings/api/available-search-engines` | engines + options + favorites |
| 8 | `list_strategies()` | in-process (hardcoded) | 5 research strategies |
| 9 | `get_configuration()` | GET `/research/api/settings/current-config` | server's current config |

**Async research trio.** For long jobs you can drive the trio directly instead
of using `detailed_research`: `start_research()` fires the job and returns
immediately; poll `research_status()`; then `fetch_report()` gets the result.
`detailed_research()` composes this trio itself and attaches a `validated`
flag so you know whether the report actually has content (the tiny server model
can return `status: completed` with empty findings).

Every function returns a **JSON string** suitable for an LLM tool call and
accepts an optional `client=` argument so a single authenticated session can be
reused across calls.

## Setup

```bash
pip install -r requirements.txt
```

Dependencies: `requests`, `beautifulsoup4`.

## Configuration

Set environment variables (never hardcode credentials):

```bash
export LDR_BASE_URL="http://192.168.0.3:1242"   # default
export LDR_USERNAME="<your-username>"
export LDR_PASSWORD="<your-password>"
export LDR_LONG_TIMEOUT=900                       # seconds for long POSTs
```

## Usage

```python
import sys
sys.path.insert(0, "/workspace/repos/ldr-openwebui-skill/tools")
import ldr_tools as t

print(t.quick_research("What is the capital of France?"))
print(t.list_strategies())
print(t.list_search_engines())
print(t.get_configuration())

# Long-running (may take many minutes):
print(t.detailed_research("History of coffee"))

# Or drive the async trio directly for very long jobs:
rid = _parse(t.start_research("History of coffee", iterations=3))
while _parse(t.research_status(rid))["status"] == "in_progress":
    time.sleep(10)
print(t.fetch_report(rid))

print(t.analyze_documents("Summarize research on coffee", collection_name="default", max_results=3))
```

Or from the command line:

```bash
cd /workspace/repos/ldr-openwebui-skill/tools
python3 -c "import ldr_tools as t; print(t.list_strategies())"
```

## Authentication

LDR does not use an API key. It authenticates with a session-cookie / CSRF-token
handshake:

1. `GET  /auth/login`      → parse `<input name="csrf_token">` from the HTML form
2. `POST /auth/login`      → form-encoded (username, password, csrf_token) → sets `session` cookie
3. `GET  /auth/csrf-token` → `{"csrf_token": "..."}` → sent as `X-CSRF-Token` header

## Retry / backoff

The login endpoint has a tight rate limiter (429). Two mitigations:

- **Single shared client.** `_SHARED_CLIENT` authenticates once and is reused
  across all tool calls.
- **429-aware retry.** `_request_with_retry` retries transient errors on GET
  requests only; POSTs are never retried on timeout (the job may already be
  running server-side).

## Server constraints

- **Login rate limiter:** wait ≥180s between login attempts.
- **Server overload:** repeated long-running POSTs can overload the server,
  causing HTTP 500 or `still_running`. Give the server recovery time between
  heavy calls.
- **Long-running POSTs:** `analyze_documents` can take many minutes; POSTs use
  `long_timeout` (default 900s).

## Dropped: `generate_report`

`POST /api/v1/generate_report` was dropped from the skill. It runs for **hours**
server-side, making it awkward from the OpenWebUI UI. The endpoint still exists
on the server; only the tool wrapper was removed.

## Project layout

```
ldr-openwebui-skill/
├── SKILL.md               ← OpenWebUI / agent skill instructions
├── README.md              ← this file
├── requirements.txt
├── .gitignore
├── tools/
│   ├── ldr_client.py      ← auth handshake + retry/backoff
│   └── ldr_tools.py       ← 7 tool functions
├── agent/
│   ├── architecture.md    ← architecture overview
│   └── progress.md        ← living log of steps taken
└── tests/
    └── test_ldr_tools.py  ← pytest suite (shared client, spaced calls)
```

## Testing

```bash
pip install pytest
LDR_BASE_URL="http://192.168.0.3:1242" LDR_USERNAME="<your-username>" LDR_PASSWORD="<your-password>" \
    python3 -m pytest tests/test_ldr_tools.py -v
```

The suite includes unit tests (no server: report validation, error helpers,
trio function existence) and live tests against the server. Live tests
authenticate once, then exercise the fast tools, `queue_status`,
`list_researches`, and the full async research trio end-to-end. The
end-to-end trio test polls `research_status()` until completion, so it can take
several minutes. Long-running tools are exercised with generous spacing to
avoid overloading the server.

## License

See [LICENSE](LICENSE).
