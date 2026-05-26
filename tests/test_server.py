"""Tests for loki_mcp/server.py — Loki LogQL query tools."""

from __future__ import annotations

import time

import pytest
import respx
from httpx import Response

from loki_mcp.server import (
    _parse_matrix,
    _parse_streams,
    _parse_time,
    get_label_values,
    get_labels,
    get_streams,
    query_aggregate,
    query_logs,
    tail_recent,
)

# ── _parse_time ───────────────────────────────────────────────────────────────


def test_parse_time_now() -> None:
    ns = _parse_time("now")
    assert abs(ns - int(time.time() * 1e9)) < 2_000_000_000  # within 2 seconds


def test_parse_time_relative_hours() -> None:
    ns = _parse_time("-1h")
    expected = int(time.time() * 1e9) - int(3600 * 1e9)
    assert abs(ns - expected) < 2_000_000_000


def test_parse_time_relative_minutes() -> None:
    ns = _parse_time("-30m")
    expected = int(time.time() * 1e9) - int(30 * 60 * 1e9)
    assert abs(ns - expected) < 2_000_000_000


def test_parse_time_iso() -> None:
    ns = _parse_time("2026-01-01T00:00:00Z")
    assert ns == 1767225600_000_000_000


def test_parse_time_invalid() -> None:
    with pytest.raises(ValueError, match="Cannot parse time expression"):
        _parse_time("not-a-time")


# ── Response parsers ──────────────────────────────────────────────────────────


def test_parse_streams_returns_flat_dicts() -> None:
    data = {
        "resultType": "streams",
        "result": [
            {
                "stream": {"agent": "dev", "job": "scoped-mcp"},
                "values": [
                    ["1700000000000000000", "first line"],
                    ["1700000001000000000", "second line"],
                ],
            }
        ],
    }
    rows = _parse_streams(data)
    assert len(rows) == 2
    assert rows[0]["line"] == "first line"
    assert rows[0]["agent"] == "dev"
    assert rows[0]["job"] == "scoped-mcp"
    assert "ts" in rows[0]


def test_parse_matrix_returns_flat_dicts() -> None:
    data = {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"agent": "research"},
                "values": [[1700000000, "42"], [1700000060, "10"]],
            }
        ],
    }
    rows = _parse_matrix(data)
    assert len(rows) == 2
    assert rows[0]["value"] == 42.0
    assert rows[0]["agent"] == "research"
    assert "ts" in rows[0]


def test_parse_streams_empty() -> None:
    assert _parse_streams({"resultType": "streams", "result": []}) == []


# ── query_logs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_query_logs_returns_parsed_lines() -> None:
    respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"agent": "dev"},
                            "values": [["1700000000000000000", "hello loki"]],
                        }
                    ],
                },
            },
        )
    )
    rows = await query_logs('{agent="dev"}', start="-1h")
    assert len(rows) == 1
    assert rows[0]["line"] == "hello loki"
    assert rows[0]["agent"] == "dev"


@pytest.mark.asyncio
@respx.mock
async def test_query_logs_unknown_result_type_returns_empty() -> None:
    respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
        return_value=Response(
            200,
            json={"status": "success", "data": {"resultType": "vector", "result": []}},
        )
    )
    rows = await query_logs('{agent="dev"}', start="-1h")
    assert rows == []


@pytest.mark.asyncio
@respx.mock
async def test_query_logs_limit_capped_at_1000() -> None:
    captured: list[dict] = []

    def capture_request(request):
        captured.append(dict(request.url.params))
        return Response(
            200,
            json={"status": "success", "data": {"resultType": "streams", "result": []}},
        )

    respx.get("http://localhost:3100/loki/api/v1/query_range").mock(side_effect=capture_request)
    await query_logs('{job="test"}', start="-1h", limit=9999)
    assert captured[0]["limit"] == "1000"


# ── query_aggregate ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_query_aggregate_returns_matrix_rows() -> None:
    respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"agent": "security"},
                            "values": [[1700000000, "5"]],
                        }
                    ],
                },
            },
        )
    )
    rows = await query_aggregate('count_over_time({agent="security"}[1h])', start="-1h")
    assert len(rows) == 1
    assert rows[0]["value"] == 5.0
    assert rows[0]["agent"] == "security"


# ── get_labels ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_labels_returns_sorted_list() -> None:
    respx.get("http://localhost:3100/loki/api/v1/labels").mock(
        return_value=Response(200, json={"status": "success", "data": ["job", "agent", "stream"]})
    )
    labels = await get_labels()
    assert labels == ["agent", "job", "stream"]


# ── get_label_values ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_label_values_returns_sorted_list() -> None:
    respx.get("http://localhost:3100/loki/api/v1/label/agent/values").mock(
        return_value=Response(
            200,
            json={"status": "success", "data": ["security", "dev", "research"]},
        )
    )
    values = await get_label_values("agent")
    assert values == ["dev", "research", "security"]


# ── get_streams ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_streams_returns_label_sets() -> None:
    respx.get("http://localhost:3100/loki/api/v1/series").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "data": [
                    {"agent": "dev", "job": "scoped-mcp"},
                    {"agent": "security", "job": "scoped-mcp"},
                ],
            },
        )
    )
    streams = await get_streams('{job="scoped-mcp"}')
    assert len(streams) == 2
    assert streams[0]["agent"] == "dev"


# ── tail_recent ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tail_recent_calls_query_logs_backward() -> None:
    captured: list[dict] = []

    def capture(request):
        captured.append(dict(request.url.params))
        return Response(
            200,
            json={"status": "success", "data": {"resultType": "streams", "result": []}},
        )

    respx.get("http://localhost:3100/loki/api/v1/query_range").mock(side_effect=capture)
    await tail_recent('{agent="dev"}', n=20)
    assert captured[0]["direction"] == "backward"
    assert captured[0]["limit"] == "20"
