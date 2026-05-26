# AGENTS.md — loki-mcp

FastMCP Python MCP server wrapping the Loki HTTP API with 6 read-only LogQL query tools.
Gives agents direct access to log streams and metrics without a Grafana session.

## What it does

Exposes six MCP tools:

- **`query_logs`** — LogQL log-stream query over a time range; returns flat log-line dicts with `ts`, `line`, and label fields
- **`query_aggregate`** — LogQL metric query (`count_over_time`, `rate`, `sum`, etc.); returns time-series dicts
- **`get_labels`** — sorted list of all label names in Loki for a given time range
- **`get_label_values`** — sorted list of values for a specific label
- **`get_streams`** — list active log streams matching a label selector
- **`tail_recent`** — convenience wrapper: most recent N lines from a stream (24h lookback, newest first)

All tools are read-only. No Loki ingest endpoints are exposed or called.

## Structure

```
loki_mcp/
  __init__.py     Package marker
  __main__.py     python -m loki_mcp entry point
  server.py       FastMCP server — all 6 tools, time helpers, response parsers
tests/
  __init__.py
  test_server.py  pytest + respx tests for all tools and helpers
ecosystem.config.js   PM2 stdio process config
pyproject.toml        Package metadata, deps, ruff + pytest config
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastmcp` | MCP server framework |
| `httpx` | Async HTTP client for Loki API calls |
| `structlog` | Structured JSON logging |

## Configuration

`LOKI_URL` environment variable — Loki base URL (default: `http://localhost:3100`).
No authentication — intended for internal forge use where Loki is not publicly exposed.

## Time parsing

`_parse_time(expr)` converts time expressions to Unix nanoseconds (Loki's native format):
- Relative: `-1h`, `-30m`, `7d` (treated as offset from now)
- ISO 8601: `2026-05-01T12:00:00Z`
- Keyword: `now`

## Response parsing

- `_parse_streams(data)` — flattens Loki `streams` resultType to `[{ts, line, ...labels}]`
- `_parse_matrix(data)` — flattens Loki `matrix` resultType to `[{ts, value, ...metric}]`

## Testing

```bash
pip install -e ".[dev]"
pytest
pytest --cov=loki_mcp --cov-report=term-missing
```

Tests use `respx` to mock the Loki HTTP API. Coverage threshold: 80%.

## Git workflow

Branch before editing — do not commit directly to `main`.
