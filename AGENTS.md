# AGENTS.md — loki-mcp

FastMCP Python MCP server wrapping the Loki HTTP API with 15 read-only tools. Gives
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
- **`get_health`** — `/ready` + buildinfo → `{ready, reason, version, revision}`; Loki down is `ready: false`, not an error
- **`get_detected_labels`** — label names on matching streams, with cardinality
- **`get_patterns`** — detected line patterns `{pattern, level, total}` (pattern ingester); pattern text is log content
- **`count_matches`** — matching-line counts per `by` group; **never returns line text**
- **`compare_windows`** — per-group `{before, after, delta, ratio}` over two windows; same builder as count_matches

All tools are read-only (`readOnlyHint`), all take an optional `tenant`, and all return
an object with `count`, `tenant` and `truncated` beside the result list.

## Structure

```
src/loki_mcp/
  __init__.py       __version__
  __main__.py       python -m loki_mcp entry point
  _client.py        config (env), tenant validation + allowlist, the one Loki request
                    path (loki_get): header, error text, timeout, OTEL span, request log
  _logql.py         counts-only query builder: selector / line_filter / by grammars
                    (count_matches, compare_windows)
  server.py         FastMCP server: 15 tools, time/step/selector checks, response
                    parsers, response-size budget, HTTP transport + bearer, main()
  observability.py  structlog setup, rotating 0600 log file, quiet third-party
                    loggers, opt-in OTEL tracer
tests/
  conftest.py              clean env per test; `env(...)` fixture
  test_client.py           config, tenants, request path, logging, tracing
  test_server.py           tools (direct and through an in-memory MCP client), parsers
  test_new_tools.py        the v0.3.0 tools
  test_logql.py            accepted/refused tables for the counts-only grammars
  test_http.py             HTTP config, bearer, /health, Host/Origin guard, real process
  test_observability.py    log file, logger levels, tracer switch
  test_live_loki.py        -m live: real multi-tenant Loki (CI job live-loki)
  loki-live-config.yaml    config for that Loki
  check_action_pins.py     CI: action pins consistent (two-sided)
  check_gitleaks_gate.py   CI: gitleaks gate fires on a planted secret (two-sided)
uv.lock               committed lock; CI and the image install from it (`uv sync --frozen`)
Dockerfile            the GHCR image (release.yml pushes it on a v* tag)
deploy/               compose example for the observability stack (not the deployed file)
ecosystem.config.js   PM2 example
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
- **`count_matches` / `compare_windows` never take a query.** They build it in `_logql.py`
  from parts that each pass a full-match grammar. Widen a grammar (a new filter operator,
  `or`, a pattern filter) only with a test that the addition cannot put line text in a
  label, and never by adding a deny-list of stage names.
- **HTTP mode stays fail-closed.** No unauthenticated path besides the exact `/health`; no
  start without a token; `/health` never calls Loki and never says more than `ok`.
- Changes to `_client.py` get a security review (CODEOWNERS lists it).

## Configuration

| Env | Default | |
|---|---|---|
| `LOKI_URL` | `http://localhost:3100` | |
| `LOKI_ORG_ID` | unset → no header | e.g. `main\|fake` |
| `LOKI_TENANTS` | unset → `tenant` refused | e.g. `main\|fake,edge` |
| `LOKI_TIMEOUT` | `30` | seconds |
| `LOKI_MAX_BODY_BYTES` | 32 MiB | read cap before parsing |
| `LOKI_MAX_LINE_CHARS` | `2000` | |
| `LOKI_MAX_RESPONSE_CHARS` | `100000` | |
| `LOG_LEVEL`, `LOG_FILE`, `OTEL_EXPORTER_OTLP_ENDPOINT` | see README | |
| `LOKI_MCP_PORT` | unset → stdio | set → streamable-http on `/mcp` |
| `LOKI_MCP_HOST` | `127.0.0.1` | non-loopback needs `LOKI_MCP_ALLOW_NONLOOPBACK=1` |
| `LOKI_MCP_API_TOKEN` | none | required in HTTP mode, ≥ 16 chars |
| `LOKI_MCP_ALLOWED_HOSTS` | `localhost:8511,127.0.0.1:8511,[::1]:8511` | Host guard; no wildcards |

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

stdio by default (an MCP client launches it). With `LOKI_MCP_PORT` it is an HTTP server;
the image (`ghcr.io/tadmstr/loki-mcp`) runs that way. See the README's "Running it" for
stdio, HTTP, PM2 and Docker. Clients that cache the tool list at session start see new
tools only in sessions started after a redeploy.

## Git workflow

Branch before editing; do not commit directly to `main`.
