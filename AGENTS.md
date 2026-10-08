# AGENTS.md — loki-mcp

FastMCP Python MCP server wrapping the Loki HTTP API with 10 read-only tools. Gives
agents direct access to log streams and metrics without a Grafana session, on
single-tenant or multi-tenant Loki.

## What it does

- **`query_logs`** — LogQL log query over a range → `{lines: [{ts, line, labels}]}`, merged across streams, newest first by default
- **`query_aggregate`** — LogQL metric query over a range → `{series: [{labels, values: [{ts, value}]}]}`
- **`query_instant`** — LogQL metric query at one time → `{samples: [{labels, ts, value}]}`
- **`get_labels`** / **`get_label_values`** — label names / values of one label
- **`get_streams`** — label sets of the streams matching a selector
- **`get_volume`** — bytes per stream or label from the index (`index/volume`, `volume_range` with `step`)
- **`get_stats`** — streams/chunks/entries/bytes from the index (excludes unflushed ingester data)
- **`get_detected_fields`** — parsed fields and structured-metadata keys in matching lines
- **`tail_recent`** — newest N lines (24 h lookback query, not a live tail)

All tools are read-only (`readOnlyHint`), all take an optional `tenant`, and all return
an object with `count`, `tenant` and `truncated` beside the result list.

## Structure

```
src/loki_mcp/
  __init__.py       __version__
  __main__.py       python -m loki_mcp entry point
  _client.py        config (env), tenant validation + allowlist, the one Loki request
                    path (loki_get): header, error text, timeout, OTEL span, request log
  server.py         FastMCP server: 10 tools, time/step/selector checks, response
                    parsers, response-size budget, main()
  observability.py  structlog setup, rotating 0600 log file, quiet third-party
                    loggers, opt-in OTEL tracer
tests/
  conftest.py              clean env per test; `env(...)` fixture
  test_client.py           config, tenants, request path, logging, tracing
  test_server.py           tools (direct and through an in-memory MCP client), parsers
  test_observability.py    log file, logger levels, tracer switch
  test_live_loki.py        -m live: real multi-tenant Loki (CI job live-loki)
  loki-live-config.yaml    config for that Loki
  check_action_pins.py     CI: action pins consistent (two-sided)
  check_gitleaks_gate.py   CI: gitleaks gate fires on a planted secret (two-sided)
uv.lock               committed lock; CI installs from it (`uv sync --frozen`)
```

## Rules for changing it

- **Every Loki request goes through `_client.loki_get`.** It sets the tenant header and
  turns Loki's error text into a `ToolError`. A tool that builds its own client loses
  both.
- **Every tool resolves its tenant with `resolve_tenant(tenant)`** and passes the result
  to `loki_get`. A per-call `tenant` must be an exact `LOKI_TENANTS` entry; never pass a
  caller-supplied value to Loki unchecked.
- **Never spread labels into a result row.** Loki merges structured metadata into stream
  labels, so whoever pushes chooses the names. Keep them under `labels`.
- **A new tool needs:** `annotations=_READ_ONLY`, a `tenant: Tenant = None` argument, its
  result through `_envelope(...)` (size budget), and a live test if it hits a new endpoint.
- **Don't log query text at INFO.** `loki_get` logs path/tenant/status/duration at INFO
  and the params at DEBUG.
- Changes to `_client.py` get a security review (CODEOWNERS lists it).

## Configuration

| Env | Default | |
|---|---|---|
| `LOKI_URL` | `http://localhost:3100` | |
| `LOKI_ORG_ID` | unset → no header | e.g. `main\|fake` |
| `LOKI_TENANTS` | unset → `tenant` refused | e.g. `main\|fake,edge` |
| `LOKI_TIMEOUT` | `30` | seconds |
| `LOKI_MAX_LINE_CHARS` | `2000` | |
| `LOKI_MAX_RESPONSE_CHARS` | `100000` | |
| `LOG_LEVEL`, `LOG_FILE`, `OTEL_EXPORTER_OTLP_ENDPOINT` | see README | |

Invalid values stop `main()` with exit 2 and the variable named.

## Testing

```bash
uv sync --frozen --extra dev --extra otel
uv run pytest --cov=loki_mcp          # floor in pyproject.toml, measured and dated
LOKI_LIVE_URL=http://127.0.0.1:3199 uv run pytest -m live   # see README for the container
```

Argument validation (`Literal`, `ge`/`le`) runs only through the MCP layer; test it with
`fastmcp.Client(mcp)`, not by calling the function.

## Deployment

Not a daemon: an MCP client launches it as a stdio subprocess. Clients that cache the
tool list at session start see new tools only in sessions started after a redeploy.

## Git workflow

Branch before editing; do not commit directly to `main`.
