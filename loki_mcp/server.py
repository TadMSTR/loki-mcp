"""loki-mcp — FastMCP server for Loki LogQL queries.

Read-only access to Loki log streams and metrics. Gives agents direct
LogQL query access without requiring a Grafana session or inline HTTP calls.

Tools:
  query_logs        — LogQL log-stream query (query_range, last N lines)
  query_aggregate   — LogQL metric query (count_over_time, rate, etc.)
  get_labels        — List all label names in Loki
  get_label_values  — List values for a specific label
  get_streams       — List active log streams matching a selector
  tail_recent       — Most recent N lines from a log stream

Configuration:
  LOKI_URL   — Loki base URL (default: http://localhost:3100)
"""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime

import httpx
import structlog
from fastmcp import FastMCP

_log = structlog.get_logger("loki-mcp")

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100").rstrip("/")

# Maximum lines returned per query — prevents oversized responses.
_MAX_LIMIT = 1000

mcp = FastMCP(
    name="loki",
    instructions=(
        "Loki MCP server. Read-only LogQL query access to log streams on forge. "
        "Use query_logs to retrieve log lines with a LogQL selector. "
        "Use query_aggregate for metric queries (count_over_time, rate, etc.). "
        "Use get_labels / get_label_values to explore available dimensions. "
        "Use get_streams to list active log streams. "
        "Use tail_recent for the most recent lines from a stream. "
        "All tools are read-only — Loki ingest endpoints are never exposed."
    ),
)

# ── Time helpers ──────────────────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"^-?(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw])$",
    re.IGNORECASE,
)
_UNIT_SECONDS: dict[str, float] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_time(expr: str) -> int:
    """Parse a time expression and return Unix nanoseconds.

    Accepts:
      - Relative durations: "-1h", "-30m", "-7d", "1h" (treated as negative from now)
      - ISO 8601 strings: "2026-05-01T12:00:00Z"
      - "now": current time
    """
    expr = expr.strip()

    if expr.lower() == "now":
        return _now_ns()

    m = _DURATION_RE.match(expr)
    if m:
        value = float(m.group("value"))
        unit = m.group("unit").lower()
        delta_s = value * _UNIT_SECONDS[unit]
        return int((time.time() - delta_s) * 1e9)

    # Try ISO 8601 parsing
    try:
        if expr.endswith("Z"):
            expr = expr[:-1] + "+00:00"
        dt = datetime.fromisoformat(expr)
        return int(dt.timestamp() * 1e9)
    except ValueError:
        pass

    raise ValueError(f"Cannot parse time expression: {expr!r}")


def _now_ns() -> int:
    return int(time.time() * 1e9)


# ── Loki response parsers ─────────────────────────────────────────────────────


def _parse_streams(data: dict) -> list[dict]:
    """Parse a streams resultType into flat log-line dicts."""
    rows: list[dict] = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        for ns_ts, line in stream.get("values", []):
            rows.append(
                {
                    "ts": datetime.fromtimestamp(int(ns_ts) / 1e9, tz=UTC).isoformat(),
                    "line": line,
                    **labels,
                }
            )
    return rows


def _parse_matrix(data: dict) -> list[dict]:
    """Parse a matrix resultType into flat metric-value dicts."""
    rows: list[dict] = []
    for series in data.get("result", []):
        metric = series.get("metric", {})
        for epoch_s, value in series.get("values", []):
            rows.append(
                {
                    "ts": datetime.fromtimestamp(float(epoch_s), tz=UTC).isoformat(),
                    "value": float(value),
                    **metric,
                }
            )
    return rows


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def query_logs(
    logql: str,
    start: str,
    end: str = "now",
    limit: int = 100,
    direction: str = "backward",
) -> list[dict]:
    """Execute a LogQL log-stream query and return matching log lines.

    Args:
        logql:     LogQL selector + pipeline, e.g. ``{agent="developer"} | json | tool=~"fs.*"``
        start:     Start time. Relative ("-1h", "-30m") or ISO 8601 timestamp.
        end:       End time. Defaults to "now".
        limit:     Maximum log lines to return (max 1000).
        direction: "backward" (newest first, default) or "forward".

    Returns:
        List of dicts with ts, line, and label fields.
    """
    limit = min(limit, _MAX_LIMIT)
    params = {
        "query": logql,
        "start": str(_parse_time(start)),
        "end": str(_parse_time(end)),
        "limit": str(limit),
        "direction": direction,
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=30) as client:
        resp = client.build_request("GET", "/loki/api/v1/query_range", params=params)
        r = await client.send(resp)
        r.raise_for_status()
        body = r.json()

    data = body.get("data", {})
    result_type = data.get("resultType", "")
    if result_type == "streams":
        return _parse_streams(data)
    _log.warning("loki_unexpected_result_type", result_type=result_type, query=logql)
    return []


@mcp.tool()
async def query_aggregate(
    logql: str,
    start: str,
    end: str = "now",
    step: str = "5m",
) -> list[dict]:
    """Execute a LogQL metric query (count_over_time, rate, sum, etc.).

    Args:
        logql:  Metric LogQL expression, e.g. ``count_over_time({agent="dev"}[1h])``
        start:  Start time. Relative ("-1h", "-7d") or ISO 8601.
        end:    End time. Defaults to "now".
        step:   Resolution step, e.g. "1m", "5m", "1h".

    Returns:
        List of dicts with ts, value, and label fields.
    """
    params = {
        "query": logql,
        "start": str(_parse_time(start)),
        "end": str(_parse_time(end)),
        "step": step,
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=30) as client:
        r = await client.get("/loki/api/v1/query_range", params=params)
        r.raise_for_status()
        body = r.json()

    data = body.get("data", {})
    result_type = data.get("resultType", "")
    if result_type == "matrix":
        return _parse_matrix(data)
    _log.warning("loki_unexpected_result_type", result_type=result_type, query=logql)
    return []


@mcp.tool()
async def get_labels(start: str = "-24h", end: str = "now") -> list[str]:
    """List all label names present in Loki within the given time range.

    Args:
        start: Start time for label discovery. Defaults to last 24 hours.
        end:   End time. Defaults to "now".

    Returns:
        Sorted list of label name strings.
    """
    params = {
        "start": str(_parse_time(start)),
        "end": str(_parse_time(end)),
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=15) as client:
        r = await client.get("/loki/api/v1/labels", params=params)
        r.raise_for_status()
        body = r.json()
    return sorted(body.get("data", []))


@mcp.tool()
async def get_label_values(
    label: str,
    start: str = "-24h",
    end: str = "now",
) -> list[str]:
    """List all values for a specific label.

    Args:
        label: Label name to query (e.g. "agent", "job", "stream").
        start: Start time for value discovery. Defaults to last 24 hours.
        end:   End time. Defaults to "now".

    Returns:
        Sorted list of value strings for the given label.
    """
    params = {
        "start": str(_parse_time(start)),
        "end": str(_parse_time(end)),
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=15) as client:
        r = await client.get(f"/loki/api/v1/label/{label}/values", params=params)
        r.raise_for_status()
        body = r.json()
    return sorted(body.get("data", []))


@mcp.tool()
async def get_streams(
    selector: str = "{}",
    start: str = "-1h",
    end: str = "now",
) -> list[dict]:
    """List active log streams matching a label selector.

    Args:
        selector: LogQL stream selector, e.g. ``{job="scoped-mcp"}`` or ``{}`` for all.
        start:    Start time for stream discovery. Defaults to last hour.
        end:      End time. Defaults to "now".

    Returns:
        List of label-set dicts, one per active stream.
    """
    params = {
        "match[]": selector,
        "start": str(_parse_time(start)),
        "end": str(_parse_time(end)),
    }
    async with httpx.AsyncClient(base_url=LOKI_URL, timeout=15) as client:
        r = await client.get("/loki/api/v1/series", params=params)
        r.raise_for_status()
        body = r.json()
    return body.get("data", [])


@mcp.tool()
async def tail_recent(
    selector: str,
    n: int = 50,
) -> list[dict]:
    """Return the most recent N log lines from a stream.

    Convenience wrapper around query_logs using a 24-hour lookback window
    and backward direction (newest first), capped at n lines.

    Args:
        selector: LogQL stream selector, e.g. ``{agent="security", stream="audit"}``
        n:        Number of recent lines to return (max 1000).

    Returns:
        List of dicts with ts, line, and label fields, newest first.
    """
    return await query_logs(
        logql=selector,
        start="-24h",
        end="now",
        limit=min(n, _MAX_LIMIT),
        direction="backward",
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
