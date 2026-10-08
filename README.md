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

Every tool is read-only (MCP `readOnlyHint`), and every tool takes an optional `tenant`.

Every result is an object: the list under a named key (`lines`, `series`, `samples`,
`labels`, `values`, `streams`, `volumes`, `fields`), plus `count`, the `tenant` header
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

Measured against Loki 3.7.6: every endpoint this server calls accepts a multi-tenant
header. Loki's `tail` and `push` do not, and this server calls neither.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOKI_URL` | `http://localhost:3100` | Loki base URL |
| `LOKI_ORG_ID` | unset (no header) | Default `X-Scope-OrgID`, e.g. `main\|fake` |
| `LOKI_TENANTS` | unset (`tenant` refused) | Allowed `tenant` values, e.g. `main\|fake,edge` |
| `LOKI_TIMEOUT` | `30` | Seconds per Loki request |
| `LOKI_MAX_LINE_CHARS` | `2000` | Longer log lines are cut and marked `line_truncated_chars` |
| `LOKI_MAX_RESPONSE_CHARS` | `100000` | Size budget for one result; beyond it items are left out and `truncated` is set |
| `LOG_LEVEL` | `INFO` | Server log level |
| `LOG_FILE` | `/opt/appdata/loki-mcp/logs/loki-mcp.log` | Rotating log (5 MB × 3, mode 600); empty for stderr only |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables one span per Loki request (needs the `otel` extra) |

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
- **The log does not record queries at INFO.** Each request logs path, tenant, status
  and duration. The LogQL text is logged only at `DEBUG`.
- **No outbound calls except Loki** (and the OTLP endpoint, if set). FastMCP's startup
  banner, which checks PyPI for updates, is off.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Installation

```bash
git clone https://github.com/TadMSTR/loki-mcp
cd loki-mcp
uv sync --frozen            # or: python -m venv .venv && .venv/bin/pip install .
```

Run it as a stdio MCP server:

```bash
LOKI_URL=http://localhost:3100 LOKI_ORG_ID='main|fake' loki-mcp
# or
python -m loki_mcp.server
```

It is meant to be launched by an MCP client as a stdio subprocess, not as a daemon.

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
  grafana/loki:3.7.6 -config.file=/etc/loki/config.yaml
LOKI_LIVE_URL=http://127.0.0.1:3199 uv run pytest -m live
```

## License

MIT. See [LICENSE](LICENSE).
