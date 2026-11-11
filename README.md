# loki-mcp

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/loki-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/loki-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

FastMCP Python MCP server for read-only Loki LogQL queries. Gives agents log stream
access without a Grafana session or inline HTTP calls. Works with single-tenant Loki
(`auth_enabled: false`) and multi-tenant Loki, including queries across several tenants.

## Tools

| Tool | Loki endpoint | Returns |
|------|---------------|---------|
| `query_logs` | `query_range` | Log lines `{ts, line, labels}`, newest first by default |
| `query_aggregate` | `query_range` | Metric series `{labels, values: [{ts, value}]}` over a range |
| `query_instant` | `query` | Metric samples `{labels, ts, value}` at one point in time |
| `get_labels` | `labels` | Label names |
| `get_label_values` | `label/<name>/values` | Values of one label |
| `get_streams` | `series` | Label sets of the matching streams |
| `get_volume` | `index/volume`, `index/volume_range` | Bytes per stream or label, largest first |
| `get_stats` | `index/stats` | Streams, chunks, entries, bytes (index estimate) |
| `get_detected_fields` | `detected_fields` | Parsed fields and structured-metadata keys, with type and cardinality |
| `tail_recent` | `query_range` | The newest N lines (24 h lookback; not a live tail) |
| `get_health` | `ready`, `status/buildinfo` | `ready`, `version`, `revision`; "Loki is down" is `ready: false`, not an error |
| `get_detected_labels` | `detected_labels` | Label names on matching streams, with cardinality |
| `get_patterns` | `patterns` | Detected line patterns `{pattern, level, total}`, most frequent first (needs the pattern ingester) |
| `count_matches` | `query` | Matching-line counts per group `{labels, count}`. **Never returns line text** |
| `compare_windows` | `query` (×2) | Per-group `{before, after, delta, ratio}` between two windows. Never returns line text |

Every tool is read-only (MCP `readOnlyHint`), and every tool takes an optional `tenant`.

Every result is an object: the list under a named key (`lines`, `series`, `samples`,
`labels`, `values`, `streams`, `volumes`, `fields`, `patterns`, `groups`), plus `count`, the `tenant` header
that was sent, and `truncated`. When the result was cut to fit the size budget, a `note`
says how much was left out and why.

## Query workflow

```
1. get_labels()                                    what dimensions exist
2. get_label_values(label="job")                   which jobs
3. get_volume(selector='{job=~".+"}')              which of them are big
4. get_detected_fields(selector='{job="docker"}')  what is inside the lines
5. query_logs(logql='{job="docker"} | logfmt | level="error"', start="-1h")
6. query_instant(logql='sum by (job) (count_over_time({job=~".+"}[15m]))')
```

To see what a stream is saying before reading it, `get_patterns(selector='{job="docker"}')`
returns its line templates (`level=warn msg="slow query" table=orders rows=<_>`) with counts.
`get_detected_labels` is the cheap first look at an unfamiliar selector.

### Finding a leak without copying it

`query_logs` with a secret pattern returns every matching line, and with it whatever leaked,
into the transcript and any trace store behind it. `count_matches` answers "is it there, and
where" with numbers only:

```
count_matches(selector='{host="argus"}',
              line_filter='|~ "(?i)(passw|token|secret|bearer)"',
              start="-24h", by="container")
→ {"groups": [{"labels": {"container": "app"}, "count": 3}], "total": 3, "query": "...", ...}

compare_windows(selector='{job="docker"}', line_filter='|= "error"',
                window_a="-48h", window_b="-24h", by="container")
→ {"groups": [{"labels": {...}, "before": 10, "after": 25, "delta": 15, "ratio": 2.5}], ...}
```

The query is built by the tool, not taken from the caller:
`sum by (<by>) (count_over_time(<selector> <line_filter> [<range>]))`. `selector` must be a
bare stream selector, `line_filter` only `|=`, `!=`, `|~`, `!~` filters with quoted
strings, and `by` label names. A parser or formatting stage (`| json`, `| logfmt`,
`| regexp`, `| pattern`, `| label_format`, `| line_format`, `| unwrap`, ...) is refused,
because it can copy line text into a label and the label would come back in the result
(measured: `| regexp "(?P<x>.*)"` grouped `by (x)` returns whole lines).

### Coming from grafana-mcp

| grafana-mcp | loki-mcp |
|---|---|
| `query_loki_logs` | `query_logs` (log queries), `query_aggregate` / `query_instant` (metric queries) |
| `query_loki_stats` | `get_stats` |
| `query_loki_patterns` | `get_patterns` |
| `list_loki_label_names` | `get_labels` |
| `list_loki_label_values` | `get_label_values` |

A metric expression sent to `query_logs`, or a log query sent to `query_aggregate` or
`query_instant`, is an error that names the right tool. LogQL errors come back with
Loki's own message (`parse error at line 1, col 21: ...`).

## Time expressions

| Format | Example | Meaning |
|--------|---------|---------|
| Relative duration | `-1h`, `-30m`, `-7d` | N units before now (`1h` means the same) |
| ISO 8601 | `2026-05-01T12:00:00Z` | Absolute. **No offset means UTC.** |
| Keyword | `now` | Current time |

Units: `s`, `m`, `h`, `d`, `w`. `step` takes a Prometheus duration (`30s`, `5m`,
`1h30m`) or a number of seconds.

## Tenants

Loki with `auth_enabled: true` answers any request without `X-Scope-OrgID` with
`401 no org id`.

- **`LOKI_ORG_ID`** is the header every call sends by default. It may name several
  tenants joined by `|` (`main|fake`); Loki then queries all of them and labels each
  result with `__tenant_id__`. Unset sends no header, which is what single-tenant Loki
  expects.
- **`LOKI_TENANTS`** is the allowlist for the per-call `tenant` argument:
  comma-separated, each entry one complete header value. With
  `LOKI_TENANTS=main|fake,edge`, `tenant="edge"` and `tenant="main|fake"` are
  allowed, and `tenant="main"` is not. Unset means `tenant` is refused.
- Tenant IDs follow Loki's rules (1–150 characters from `0-9 A-Z a-z ! - _ . * ' ( )`).
  **A colon is rejected**: Loki stops reading the header at a colon and serves the part
  before it, so `main:x` would silently read tenant `main`.

Measured against Loki 3.7.8: every endpoint this server calls accepts a multi-tenant
header (`/ready` and buildinfo ignore it). Loki's `tail` and `push` do not, and this server
calls neither. `get_patterns` with several tenants returns their patterns merged, with no
per-tenant marker.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOKI_URL` | `http://localhost:3100` | Loki base URL |
| `LOKI_ORG_ID` | unset (no header) | Default `X-Scope-OrgID`, e.g. `main\|fake` |
| `LOKI_TENANTS` | unset (`tenant` refused) | Allowed `tenant` values, e.g. `main\|fake,edge` |
| `LOKI_TIMEOUT` | `30` | Seconds per Loki request |
| `LOKI_MAX_BODY_BYTES` | `33554432` (32 MiB) | Largest Loki response read; a bigger one is refused before parsing |
| `LOKI_MAX_LINE_CHARS` | `2000` | Longer log lines are cut and marked `line_truncated_chars` |
| `LOKI_MAX_RESPONSE_CHARS` | `100000` | Size budget for one result; beyond it items are left out and `truncated` is set |
| `LOG_LEVEL` | `INFO` | Server log level |
| `LOG_FILE` | `/opt/appdata/loki-mcp/logs/loki-mcp.log` | Rotating log (5 MB × 3, every file mode 600); empty for stderr only |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables one span per Loki request (needs the `otel` extra) |
| `LOKI_MCP_PORT` | unset (stdio) | Set: serve streamable-http on `/mcp` at this port |
| `LOKI_MCP_HOST` | `127.0.0.1` | HTTP bind address. The image sets `0.0.0.0` |
| `LOKI_MCP_ALLOW_NONLOOPBACK` | unset | Required (`1`) to bind a non-loopback address. The image sets it |
| `LOKI_MCP_API_TOKEN` | none | Bearer token. **Required in HTTP mode**, at least 16 characters |
| `LOKI_MCP_ALLOWED_HOSTS` | `localhost:8511,127.0.0.1:8511,[::1]:8511` | Host values the DNS-rebinding guard accepts. No wildcards |

An invalid value stops the server at startup with the variable named, rather than on
an agent's first call.

## Security model

- **Read-only.** Only `GET` requests to Loki's query endpoints. No push, delete or
  admin endpoints are implemented.
- **Tenant access is an allowlist.** With `LOKI_TENANTS` unset, an agent can only read
  the tenant(s) in `LOKI_ORG_ID`.
- **Untrusted label names cannot replace data.** Labels are returned under `labels`,
  never spread into the row, so a pushed label or structured-metadata key called
  `line`, `ts` or `value` cannot overwrite the log line, timestamp or sample.
- **Bounded reads.** Loki's response is read up to `LOKI_MAX_BODY_BYTES` before it is
  parsed, and the result handed to the agent is cut to `LOKI_MAX_RESPONSE_CHARS`.
- **Errors.** A 4xx from Loki (a LogQL error, a bad parameter) is returned with Loki's
  message, because the agent needs it to fix the query. A 5xx is returned as a generic
  server-side error and its body goes to the log. Credentials in `LOKI_URL` are never
  echoed.
- **The log does not record queries at INFO.** Each request logs path, tenant, status
  and duration. The LogQL text is logged only at `DEBUG`.
- **`count_matches` and `compare_windows` never return line text.** See
  [Finding a leak without copying it](#finding-a-leak-without-copying-it). Stream labels
  and structured metadata named in `by` do come back: they are label values, as in
  `get_label_values`.
- **Pattern text is log content.** `get_patterns` cuts it at `LOKI_MAX_LINE_CHARS` and it
  should be treated like a log line.
- **HTTP mode fails closed.** A bearer token is mandatory, compared in constant time; a
  non-loopback bind needs `LOKI_MCP_ALLOW_NONLOOPBACK=1`; `/health` (exact path) is the only
  unauthenticated route and returns `{"status":"ok"}` and nothing else. FastMCP's Host/Origin
  guard is on: a foreign `Host` is refused with 421 before the token is checked, a foreign
  `Origin` with 403.
- **No outbound calls except Loki** (and the OTLP endpoint, if set). FastMCP's startup
  banner, which checks PyPI for updates, is off.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Running it

The same variables work in every mode.

### stdio (an MCP client launches it)

```bash
git clone https://github.com/TadMSTR/loki-mcp && cd loki-mcp
uv sync --frozen            # or: python -m venv .venv && .venv/bin/pip install '.[otel]'
LOKI_URL=http://localhost:3100 LOKI_ORG_ID='main|fake' .venv/bin/loki-mcp
```

This is the default: with `LOKI_MCP_PORT` unset the server speaks MCP on stdin/stdout.

### HTTP on the host

```bash
export LOKI_MCP_API_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
LOKI_MCP_PORT=8511 LOKI_URL=http://localhost:3100 .venv/bin/loki-mcp
```

Clients connect to `http://localhost:8511/mcp` with `Authorization: Bearer <token>`.
`GET /health` answers without a token and never calls Loki; the `get_health` tool is what
checks Loki. If clients reach it under another hostname or port, list the `Host` values in
`LOKI_MCP_ALLOWED_HOSTS`.

### PM2

[`ecosystem.config.js`](ecosystem.config.js) runs the HTTP mode above under PM2. Export
`LOKI_MCP_API_TOKEN` in the shell first; it does not go in the file.

```bash
pm2 start ecosystem.config.js
```

### Docker

The image is `ghcr.io/tadmstr/loki-mcp:<version>` (amd64 and arm64). Each GitHub release
lists its digest; pin tag and digest. It serves HTTP on 8511 as uid 1000, logs to stderr,
and runs with a read-only root filesystem.

```bash
docker run -d --name loki-mcp --read-only --cap-drop ALL \
  --security-opt no-new-privileges -p 127.0.0.1:8511:8511 \
  -e LOKI_URL=http://loki:3100 -e LOKI_MCP_API_TOKEN="$LOKI_MCP_API_TOKEN" \
  --network <the network Loki is on> ghcr.io/tadmstr/loki-mcp:v0.3.0
```

[`deploy/docker-compose.loki-mcp.yml`](deploy/docker-compose.loki-mcp.yml) is a commented
compose service. Two traps it explains: `LOKI_URL` must name Loki's container, not
`localhost` (inside the container that is its own loopback, and the server still reports
healthy while every tool fails), and the published port is the exposure control, not the
in-container bind address. For stdio from the image, `docker run -i -e LOKI_MCP_PORT= ...`.

### Which should I pick?

- **An MCP client on the same machine that launches servers itself:** stdio.
- **Host-coupled** (needs the host's files or processes): PM2. loki-mcp is not.
- **An API client** of a service that already runs in a container, which loki-mcp is: Docker,
  next to Loki, published on loopback. It is portable, resource-limited, and reachable from
  other containers.

## Development

```bash
uv sync --frozen --extra dev --extra otel
uv run pytest --cov=loki_mcp            # unit tests, coverage floor in pyproject.toml
uv run ruff check . && uv run ruff format --check .
```

Live tests against a real multi-tenant Loki (CI runs them in the `live-loki` job):

```bash
docker run -d --rm --name loki-live -p 127.0.0.1:3199:3100 \
  -v "$PWD/tests/loki-live-config.yaml:/etc/loki/config.yaml:ro" \
  grafana/loki:3.7.8 -config.file=/etc/loki/config.yaml
LOKI_LIVE_URL=http://127.0.0.1:3199 uv run pytest -m live
```

## License

MIT. See [LICENSE](LICENSE).
