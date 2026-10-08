"""loki-mcp — FastMCP server for Loki LogQL queries.

Read-only access to Loki log streams and metrics. Gives agents direct
LogQL query access without requiring a Grafana session or inline HTTP calls.

Tools:
  query_logs           — LogQL log query over a range; log lines, newest first by default
  query_aggregate      — LogQL metric query over a range (count_over_time, rate, ...)
  query_instant        — LogQL metric query at one point in time
  get_labels           — label names
  get_label_values     — values of one label
  get_streams          — label sets of the streams matching a selector
  get_volume           — bytes ingested per stream or label (index/volume[_range])
  get_stats            — streams, chunks, entries, bytes for a selector (index/stats)
  get_detected_fields  — fields and structured-metadata keys found in matching lines
  tail_recent          — most recent N lines from a selector

Every tool takes an optional ``tenant``; see _client.py for LOKI_ORG_ID / LOKI_TENANTS.

Output limits:
  LOKI_MAX_LINE_CHARS      longest log line returned before it is cut (default 2000)
  LOKI_MAX_RESPONSE_CHARS  total size budget for one tool result (default 100000)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import _client
from ._client import ConfigError, loki_get, resolve_tenant

_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Maximum lines returned per query.
_MAX_LIMIT = 1000

mcp = FastMCP(
    name="loki",
    instructions=(
        "Loki MCP server. Read-only LogQL query access to Loki log streams. "
        "Use query_logs for log lines, query_aggregate for a metric over a range, "
        "query_instant for a metric at one point in time. "
        "Use get_labels / get_label_values / get_streams to explore what exists, "
        "get_volume and get_stats to size it, get_detected_fields to see which fields a "
        "selector's lines carry. tail_recent returns the newest lines from a selector. "
        "Every tool takes an optional tenant (an allowed X-Scope-OrgID value); omit it to "
        "use the server's default. Results that span several tenants carry a "
        "__tenant_id__ label. All tools are read-only."
    ),
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

Tenant = Annotated[
    str | None,
    Field(
        description=(
            "X-Scope-OrgID for this call, e.g. 'edge' or 'main|fake'. Must be one of the "
            "server's LOKI_TENANTS values. Omit to use the default (LOKI_ORG_ID)."
        )
    ),
]
Limit = Annotated[int, Field(ge=1, le=_MAX_LIMIT)]


# ── Output limits ─────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: {raw!r} is not an integer.") from None
    if value < 1:
        raise ConfigError(f"{name}: must be at least 1, got {value}.")
    return value


def _max_line_chars() -> int:
    return _env_int("LOKI_MAX_LINE_CHARS", 2000)


def _max_response_chars() -> int:
    return _env_int("LOKI_MAX_RESPONSE_CHARS", 100_000)


def _within_budget(items: list[Any], size: Callable[[Any], int]) -> tuple[list[Any], int]:
    """Keep items, in order, until the response budget is spent. Return (kept, omitted).

    The first item is always kept, so an over-budget result is never silently empty.
    """
    budget = _max_response_chars()
    kept: list[Any] = []
    used = 0
    for item in items:
        used += size(item)
        if used > budget and kept:
            break
        kept.append(item)
    return kept, len(items) - len(kept)


def _json_size(item: Any) -> int:
    return len(json.dumps(item, ensure_ascii=False, default=str))


def _envelope(key: str, items: list[Any], tenant: str | None) -> dict[str, Any]:
    """Wrap a result list, cutting it to the response budget and saying so if it was."""
    kept, omitted = _within_budget(items, _json_size)
    out: dict[str, Any] = {key: kept, "count": len(kept), "tenant": tenant}
    out["truncated"] = omitted > 0
    if omitted:
        out["note"] = (
            f"{omitted} of {len(items)} {key} omitted: the result exceeded "
            f"LOKI_MAX_RESPONSE_CHARS={_max_response_chars()}. Narrow the selector or "
            "time range, or lower limit."
        )
    return out


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

# Loki's `step`: a Prometheus duration ("5m", "1h30m") or a number of seconds ("60").
_STEP_RE = re.compile(r"^(?:\d+(?:\.\d+)?|(?:\d+(?:ms|s|m|h|d|w|y))+)$")


def _parse_time(expr: str) -> int:
    """Parse a time expression and return Unix nanoseconds.

    Accepts:
      - Relative durations: "-1h", "-30m", "-7d", "1h" (treated as negative from now)
      - ISO 8601 strings: "2026-05-01T12:00:00Z". No offset means UTC.
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

    try:
        iso = expr[:-1] + "+00:00" if expr.endswith(("Z", "z")) else expr
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ToolError(
            f"Cannot parse time expression: {expr!r}. Use 'now', a relative duration such "
            "as '-1h', or an ISO 8601 timestamp."
        ) from None
    if dt.tzinfo is None:
        # Before 0.2.0 a timestamp without an offset was read as the server's local time,
        # hours out for an agent that meant UTC on any host not running in UTC.
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def _now_ns() -> int:
    return time.time_ns()


def _range(start: str, end: str) -> dict[str, str]:
    s, e = _parse_time(start), _parse_time(end)
    if s >= e:
        raise ToolError(f"start ({start!r}) must be before end ({end!r}).")
    return {"start": str(s), "end": str(e)}


def _check_step(step: str) -> str:
    step = step.strip()
    if not _STEP_RE.match(step) or not re.search(r"[1-9]", step):
        raise ToolError(
            f"Invalid step {step!r}. Use a positive duration such as '30s', '5m', '1h' or "
            "a number of seconds."
        )
    return step


def _check_selector(selector: str) -> str:
    selector = selector.strip()
    if not selector.startswith("{"):
        raise ToolError(
            f"Expected a stream selector such as '{{job=\"docker\"}}', got {selector!r}."
        )
    return selector


# ── Loki response parsers ─────────────────────────────────────────────────────


def _iso_ns(ns: int) -> str:
    secs, rem = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(secs, tz=UTC).replace(microsecond=rem // 1000).isoformat()


def _iso_s(epoch_s: float | str) -> str:
    return datetime.fromtimestamp(float(epoch_s), tz=UTC).isoformat()


def _number(raw: str) -> float | str:
    """Loki sends sample values as strings. Return a float, or Loki's own string for
    NaN / +Inf / -Inf: FastMCP serialises a non-finite float as null (measured on 4.1.0),
    which reads as "no data" rather than "division by zero"."""
    value = float(raw)
    return value if math.isfinite(value) else raw


def _parse_streams(data: dict, direction: str = "backward") -> list[dict]:
    """Parse a streams result into ``[{ts, line, labels}]``, merged across streams.

    Labels are nested, not spread into the row. Spread (0.1.x), a stream label or
    structured-metadata key named ``line`` or ``ts`` replaced the real log line or
    timestamp. Loki returns structured metadata merged into the stream labels, so
    anyone who can push can choose those names.

    Loki applies ``limit`` across all streams but returns entries grouped by stream, so
    the rows are re-sorted here; 0.1.x concatenated the groups, and "newest first" held
    only within each stream.
    """
    max_line = _max_line_chars()
    keyed: list[tuple[int, dict]] = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        for value in stream.get("values", []):
            ns, line = int(value[0]), value[1]
            row: dict[str, Any] = {"ts": _iso_ns(ns), "line": line, "labels": labels}
            if len(line) > max_line:
                row["line"] = line[:max_line]
                row["line_truncated_chars"] = len(line) - max_line
            keyed.append((ns, row))
    keyed.sort(key=lambda kr: kr[0], reverse=(direction == "backward"))
    return [row for _, row in keyed]


def _parse_matrix(data: dict) -> list[dict]:
    """Parse a matrix result into ``[{labels, values: [{ts, value}]}]``, one per series.

    Labels are nested for the same reason as in ``_parse_streams``: spread, a label named
    ``value`` replaced the sample.
    """
    return [
        {
            "labels": s.get("metric", {}),
            "values": [{"ts": _iso_s(ts), "value": _number(v)} for ts, v in s.get("values", [])],
        }
        for s in data.get("result", [])
    ]


def _parse_vector(data: dict) -> list[dict]:
    """Parse a vector (or scalar) result into ``[{labels, ts, value}]``."""
    if data.get("resultType") == "scalar":
        ts, v = data.get("result", [0, "NaN"])
        return [{"labels": {}, "ts": _iso_s(ts), "value": _number(v)}]
    return [
        {
            "labels": s.get("metric", {}),
            "ts": _iso_s(s["value"][0]),
            "value": _number(s["value"][1]),
        }
        for s in data.get("result", [])
    ]


def _wrong_shape(tool: str, result_type: str) -> ToolError:
    if result_type == "streams":
        hint = "This is a log query; use query_logs (or tail_recent)."
    elif result_type in ("matrix", "vector", "scalar"):
        hint = (
            "This is a metric query; use query_aggregate for a range or query_instant for "
            "one point in time."
        )
    else:
        hint = "Loki returned a result type this tool does not handle."
    return ToolError(f"{tool}: Loki returned resultType {result_type!r}. {hint}")


def _data(body: Any) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _list(body: Any) -> list:
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, list) else []


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool(annotations=_READ_ONLY)
async def query_logs(
    logql: str,
    start: str,
    end: str = "now",
    limit: Limit = 100,
    direction: Literal["backward", "forward"] = "backward",
    tenant: Tenant = None,
) -> dict:
    """Execute a LogQL log query and return matching log lines.

    Args:
        logql:     LogQL selector + pipeline, e.g. ``{job="scoped-mcp"} | json | tool=~"fs.*"``
        start:     Start time. Relative ("-1h", "-30m") or ISO 8601 (no offset = UTC).
        end:       End time. Defaults to "now".
        limit:     Maximum log lines to return, 1-1000.
        direction: "backward" (newest first, default) or "forward" (oldest first).
        tenant:    Optional X-Scope-OrgID for this call.

    Returns:
        ``{"lines": [{"ts", "line", "labels"}], "count", "tenant", "truncated"}``. A line
        longer than LOKI_MAX_LINE_CHARS is cut and carries ``line_truncated_chars``.
        A metric expression is an error that names the right tool.
    """
    params = {"query": logql, **_range(start, end), "limit": str(limit), "direction": direction}
    org = resolve_tenant(tenant)
    data = _data(await loki_get("/loki/api/v1/query_range", params, org))
    result_type = data.get("resultType", "")
    if result_type != "streams":
        raise _wrong_shape("query_logs", result_type)
    return _envelope("lines", _parse_streams(data, direction), org)


@mcp.tool(annotations=_READ_ONLY)
async def query_aggregate(
    logql: str,
    start: str,
    end: str = "now",
    step: str = "5m",
    tenant: Tenant = None,
) -> dict:
    """Execute a LogQL metric query over a time range (count_over_time, rate, sum, ...).

    Args:
        logql:  Metric LogQL, e.g. ``sum by (job) (count_over_time({job=~".+"}[5m]))``
        start:  Start time. Relative ("-1h", "-7d") or ISO 8601 (no offset = UTC).
        end:    End time. Defaults to "now".
        step:   Resolution step, e.g. "1m", "5m", "1h", or seconds.
        tenant: Optional X-Scope-OrgID for this call.

    Returns:
        ``{"series": [{"labels", "values": [{"ts", "value"}]}], "count", "tenant",
        "truncated"}``. ``value`` is a number, or Loki's string for NaN / +Inf / -Inf.
    """
    params = {"query": logql, **_range(start, end), "step": _check_step(step)}
    org = resolve_tenant(tenant)
    data = _data(await loki_get("/loki/api/v1/query_range", params, org))
    result_type = data.get("resultType", "")
    if result_type != "matrix":
        raise _wrong_shape("query_aggregate", result_type)
    return _envelope("series", _parse_matrix(data), org)


@mcp.tool(annotations=_READ_ONLY)
async def query_instant(
    logql: str,
    at: str = "now",
    tenant: Tenant = None,
) -> dict:
    """Evaluate a LogQL metric query at one point in time — the shape alert rules use.

    Example, "how many edge lines in the last 15 minutes":
    ``sum(count_over_time({host="edge"}[15m]))``.

    Args:
        logql:  Metric LogQL expression. A log query is an error (use query_logs).
        at:     Evaluation time. Defaults to "now".
        tenant: Optional X-Scope-OrgID for this call.

    Returns:
        ``{"samples": [{"labels", "ts", "value"}], "count", "tenant", "truncated"}``.
    """
    params = {"query": logql, "time": str(_parse_time(at))}
    org = resolve_tenant(tenant)
    data = _data(await loki_get("/loki/api/v1/query", params, org))
    result_type = data.get("resultType", "")
    if result_type not in ("vector", "scalar"):
        raise _wrong_shape("query_instant", result_type)
    return _envelope("samples", _parse_vector(data), org)


@mcp.tool(annotations=_READ_ONLY)
async def get_labels(start: str = "-24h", end: str = "now", tenant: Tenant = None) -> dict:
    """List the label names present in Loki within the given time range.

    Args:
        start:  Start time. Defaults to the last 24 hours.
        end:    End time. Defaults to "now".
        tenant: Optional X-Scope-OrgID for this call.

    Returns:
        ``{"labels": [sorted names], "count", "tenant", "truncated"}``.
    """
    params = _range(start, end)
    org = resolve_tenant(tenant)
    body = await loki_get("/loki/api/v1/labels", params, org)
    return _envelope("labels", sorted(_list(body)), org)


@mcp.tool(annotations=_READ_ONLY)
async def get_label_values(
    label: str,
    start: str = "-24h",
    end: str = "now",
    tenant: Tenant = None,
) -> dict:
    """List the values of one label.

    Args:
        label:  Label name (e.g. "job", "container", "host").
        start:  Start time. Defaults to the last 24 hours.
        end:    End time. Defaults to "now".
        tenant: Optional X-Scope-OrgID for this call.

    Returns:
        ``{"values": [sorted values], "count", "tenant", "truncated"}``.
    """
    if not _LABEL_RE.match(label):
        raise ToolError(f"Invalid label name: {label!r}")
    params = _range(start, end)
    org = resolve_tenant(tenant)
    body = await loki_get(f"/loki/api/v1/label/{label}/values", params, org)
    return _envelope("values", sorted(_list(body)), org)


@mcp.tool(annotations=_READ_ONLY)
async def get_streams(
    selector: str = "{}",
    start: str = "-1h",
    end: str = "now",
    tenant: Tenant = None,
) -> dict:
    """List the label sets of the streams matching a selector.

    Args:
        selector: Stream selector, e.g. ``{job="scoped-mcp"}``, or ``{}`` for all.
        start:    Start time. Defaults to the last hour.
        end:      End time. Defaults to "now".
        tenant:   Optional X-Scope-OrgID for this call.

    Returns:
        ``{"streams": [label-set dicts], "count", "tenant", "truncated"}``.
    """
    params = {"match[]": _check_selector(selector), **_range(start, end)}
    org = resolve_tenant(tenant)
    body = await loki_get("/loki/api/v1/series", params, org)
    return _envelope("streams", _list(body), org)


@mcp.tool(annotations=_READ_ONLY)
async def get_volume(
    selector: str,
    start: str = "-1h",
    end: str = "now",
    limit: Limit = 100,
    target_labels: list[str] | None = None,
    aggregate_by: Literal["series", "labels"] = "series",
    step: str | None = None,
    tenant: Tenant = None,
) -> dict:
    """Bytes ingested per stream (or per label) over a range, largest first.

    Reads Loki's index (``/index/volume``), so it is cheap even over long ranges. Lines
    newer than about a second before ``end`` are not counted yet (measured on Loki 3.7.6).

    Args:
        selector:      Stream selector, e.g. ``{host="edge"}`` or ``{job=~".+"}``.
        start:         Start time. Defaults to the last hour.
        end:           End time. Defaults to "now".
        limit:         Maximum series to return, 1-1000.
        target_labels: Aggregate into these labels only, e.g. ["job"].
        aggregate_by:  "series" (label-value combinations, default) or "labels" (label
                       names only: which labels carry the most data).
        step:          If set, return a series over time (``/index/volume_range``).
        tenant:        Optional X-Scope-OrgID for this call.

    Returns:
        ``{"volumes": [{"labels", "bytes"}], ...}``, or with step and more than one step
        in the range, ``{"series": [{"labels", "values": [{"ts", "value"}]}], ...}``.
    """
    params = {
        "query": _check_selector(selector),
        **_range(start, end),
        "limit": str(limit),
        "aggregateBy": aggregate_by,
    }
    if target_labels:
        bad = [t for t in target_labels if not _LABEL_RE.match(t)]
        if bad:
            raise ToolError(f"Invalid label name(s) in target_labels: {bad!r}")
        params["targetLabels"] = ",".join(target_labels)
    path = "/loki/api/v1/index/volume"
    if step is not None:
        params["step"] = _check_step(step)
        path = "/loki/api/v1/index/volume_range"
    org = resolve_tenant(tenant)
    data = _data(await loki_get(path, params, org))
    result_type = data.get("resultType", "")
    # volume_range is documented as always returning a matrix. Measured on Loki 3.7.6
    # (2026-10-08) it returned a vector for a range holding one step of data, so both
    # shapes are handled on both paths.
    if result_type == "matrix":
        return _envelope("series", _parse_matrix(data), org)
    if result_type == "vector":
        rows = [{"labels": r["labels"], "bytes": r["value"]} for r in _parse_vector(data)]
        rows.sort(key=lambda r: r["bytes"] if isinstance(r["bytes"], float) else -1.0)
        rows.reverse()
        return _envelope("volumes", rows, org)
    raise _wrong_shape("get_volume", result_type)


@mcp.tool(annotations=_READ_ONLY)
async def get_stats(
    selector: str,
    start: str = "-1h",
    end: str = "now",
    tenant: Tenant = None,
) -> dict:
    """Streams, chunks, entries and bytes a selector resolves to, from Loki's index.

    An approximation (Loki docs), and it does NOT include data still in the ingesters:
    measured on Loki 3.7.6, freshly pushed lines read as 0 until their chunk flushed.
    For "how many lines in the last few minutes" use query_instant with count_over_time.
    With a multi-tenant value the counts are summed across the tenants.

    Args:
        selector: Stream selector, e.g. ``{host="edge"}``.
        start:    Start time. Defaults to the last hour.
        end:      End time. Defaults to "now".
        tenant:   Optional X-Scope-OrgID for this call.

    Returns:
        ``{"streams", "chunks", "entries", "bytes", "tenant", "note"}``.
    """
    params = {"query": _check_selector(selector), **_range(start, end)}
    org = resolve_tenant(tenant)
    body = await loki_get("/loki/api/v1/index/stats", params, org)
    if not isinstance(body, dict):
        raise ToolError("get_stats: Loki returned an unexpected body.")
    return {
        "streams": body.get("streams", 0),
        "chunks": body.get("chunks", 0),
        "entries": body.get("entries", 0),
        "bytes": body.get("bytes", 0),
        "tenant": org,
        "note": "Index estimate; excludes data not yet flushed from the ingesters.",
    }


@mcp.tool(annotations=_READ_ONLY)
async def get_detected_fields(
    selector: str,
    start: str = "-1h",
    end: str = "now",
    line_limit: Limit = 100,
    tenant: Tenant = None,
) -> dict:
    """Fields Loki detects in the lines a selector matches: parsed (json, logfmt) fields
    and structured-metadata keys, with type, estimated cardinality and parser.

    Structured-metadata keys show ``parsers: null``.

    Args:
        selector:   Stream selector, e.g. ``{host="edge"}``.
        start:      Start time. Defaults to the last hour.
        end:        End time. Defaults to "now".
        line_limit: Lines Loki samples per shard, 1-1000 (Loki's default is 100).
        tenant:     Optional X-Scope-OrgID for this call.

    Returns:
        ``{"fields": [{"label", "type", "cardinality", "parsers", ...}], "count",
        "tenant", "truncated"}``.
    """
    params = {
        "query": _check_selector(selector),
        **_range(start, end),
        "line_limit": str(line_limit),
    }
    org = resolve_tenant(tenant)
    body = await loki_get("/loki/api/v1/detected_fields", params, org)
    fields = body.get("fields") if isinstance(body, dict) else None
    return _envelope("fields", list(fields or []), org)


@mcp.tool(annotations=_READ_ONLY)
async def tail_recent(
    selector: str,
    n: Limit = 50,
    tenant: Tenant = None,
) -> dict:
    """Return the most recent N log lines from a selector, newest first.

    A 24-hour lookback query, not a live tail (Loki's /tail refuses multi-tenant
    headers and needs a websocket).

    Args:
        selector: LogQL selector, e.g. ``{job="scoped-mcp", stream="audit"}``
        n:        Number of lines, 1-1000.
        tenant:   Optional X-Scope-OrgID for this call.

    Returns:
        Same shape as query_logs.
    """
    return await query_logs(
        logql=selector,
        start="-24h",
        end="now",
        limit=n,
        direction="backward",
        tenant=tenant,
    )


def _validate_startup() -> None:
    """Fail at launch, with the variable named, rather than on an agent's first call."""
    _client.reset_config()
    _client.config()
    _max_line_chars()
    _max_response_chars()


def main() -> None:
    from .observability import configure_logging

    configure_logging()
    try:
        _validate_startup()
    except ConfigError as exc:
        print(f"loki-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    # show_banner=False: the banner is what runs FastMCP's update check, a GET to
    # pypi.org from inside a stdio server on every start.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
