# loki-mcp

FastMCP Python MCP server for read-only Loki LogQL queries. Gives agents direct
log stream access without requiring a Grafana session or inline HTTP calls.

## Tool Reference

| Tool | Description |
|------|-------------|
| `query_logs` | LogQL log-stream query — returns matching log lines with timestamps and labels |
| `query_aggregate` | LogQL metric query — `count_over_time`, `rate`, `sum`, etc. |
| `get_labels` | List all label names present in Loki |
| `get_label_values` | List all values for a specific label |
| `get_streams` | List active log streams matching a label selector |
| `tail_recent` | Most recent N lines from a stream (24h lookback, newest first) |

## Query Workflow

```
1. get_labels()
   → discover available label names (e.g. "agent", "job", "stream")

2. get_label_values(label="agent")
   → find specific values (e.g. "developer", "security", "homelab-ops")

3. query_logs(logql='{agent="developer"} | json', start="-1h")
   → retrieve matching log lines

4. query_aggregate(logql='count_over_time({agent="developer"}[5m])', start="-1h")
   → metric rollup over time
```

## Time Expression Formats

All time parameters accept:

| Format | Example | Meaning |
|--------|---------|---------|
| Relative duration | `-1h`, `-30m`, `-7d` | N units before now |
| ISO 8601 | `2026-05-01T12:00:00Z` | Absolute timestamp |
| Keyword | `now` | Current time |

Units: `s` (seconds), `m` (minutes), `h` (hours), `d` (days), `w` (weeks).

## Security Model

**What this MCP can access:**
- Loki HTTP API at the configured `LOKI_URL` — read-only query endpoints only

**What this MCP cannot access:**
- Loki ingest endpoints (`/loki/api/v1/push`, `/loki/api/v1/write`) — not implemented
- Any filesystem path or other service

**Read-only guarantee:**
All tool calls use GET requests. No POST, PUT, or DELETE operations are implemented.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `LOKI_URL` | no | `http://localhost:3100` | Loki base URL |

## Deployment (forge, PM2)

```bash
cd ~/repos/personal
git clone <repo-url> loki-mcp

cd loki-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

pm2 start ecosystem.config.js
pm2 save
```

Loki must be reachable at `LOKI_URL`. On forge, `127.0.0.1:3100` must be published
in the Loki Docker Compose before loki-mcp can connect.

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov=loki_mcp
```
