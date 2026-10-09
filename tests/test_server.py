"""Tests for server.py — the tools, the parsers and the time helpers.

Tool tests go through an in-memory FastMCP client where argument validation matters,
because that is the path an agent takes: calling the Python function directly skips the
schema (Literal, ge/le) that rejects bad arguments.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from loki_mcp import server
from loki_mcp.server import (
    _check_step,
    _parse_matrix,
    _parse_streams,
    _parse_time,
    _parse_vector,
    get_detected_fields,
    get_label_values,
    get_labels,
    get_stats,
    get_streams,
    get_volume,
    mcp,
    query_aggregate,
    query_instant,
    query_logs,
    tail_recent,
)

from .conftest import LOKI

QR = f"{LOKI}/loki/api/v1/query_range"


def _streams(*streams: tuple[dict, list[tuple[int, str]]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": labels, "values": [[str(ns), line] for ns, line in values]}
                for labels, values in streams
            ],
        },
    }


def _ok(result_type: str, result) -> dict:
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


async def _call(name: str, args: dict):
    async with Client(mcp) as c:
        return await c.call_tool(name, args, raise_on_error=False)


# ── _parse_time ───────────────────────────────────────────────────────────────


def test_parse_time_now() -> None:
    assert abs(_parse_time("now") - time.time_ns()) < 2_000_000_000


@pytest.mark.parametrize(("expr", "seconds"), [("-1h", 3600), ("-30m", 1800), ("2d", 172800)])
def test_parse_time_relative(expr: str, seconds: int) -> None:
    expected = time.time_ns() - seconds * 1_000_000_000
    assert abs(_parse_time(expr) - expected) < 2_000_000_000


def test_parse_time_iso_z() -> None:
    assert _parse_time("2026-01-01T00:00:00Z") == 1767225600_000_000_000


def test_parse_time_iso_offset() -> None:
    assert _parse_time("2026-01-01T00:00:00-05:00") == 1767243600_000_000_000


def test_parse_time_iso_keeps_microseconds() -> None:
    assert _parse_time("2026-01-01T00:00:00.123456Z") == 1767225600_123_456_000


def test_parse_time_naive_iso_is_utc_not_local(monkeypatch) -> None:
    # Hypothesis 7: 0.1.x read this as local time, 5 h out under America/New_York.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert _parse_time("2026-01-01T00:00:00") == 1767225600_000_000_000
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_parse_time_invalid() -> None:
    with pytest.raises(ToolError, match="Cannot parse time expression"):
        _parse_time("not-a-time")


async def test_start_after_end_rejected() -> None:
    with pytest.raises(ToolError, match="must be before end"):
        await get_labels(start="now", end="-1h")


# ── _check_step ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("step", ["30s", "5m", "1h30m", "60", "0.5", "250ms", "1d"])
def test_step_accepted(step: str) -> None:
    assert _check_step(step) == step


@pytest.mark.parametrize("step", ["banana", "0", "0s", "-5m", "5 m", "", "5M"])
def test_step_rejected(step: str) -> None:
    # Hypothesis 5: 0.1.x sent any step to Loki.
    with pytest.raises(ToolError, match="Invalid step"):
        _check_step(step)


# ── Parsers ───────────────────────────────────────────────────────────────────


def test_parse_streams_nests_labels() -> None:
    rows = _parse_streams(_streams(({"job": "j"}, [(1700000000_000000000, "hello")]))["data"])
    assert rows == [{"ts": "2023-11-14T22:13:20+00:00", "line": "hello", "labels": {"job": "j"}}]


def test_label_named_line_or_ts_cannot_replace_the_log_line() -> None:
    # Hypothesis 1: 0.1.x spread labels after ts/line, so these replaced the real values.
    # Loki merges structured metadata into stream labels, so a pusher picks these names.
    data = _streams(({"line": "FORGED", "ts": "FORGED", "job": "j"}, [(1, "real line")]))
    [row] = _parse_streams(data["data"])
    assert row["line"] == "real line"
    assert row["ts"].startswith("1970-01-01")
    assert row["labels"]["line"] == "FORGED"


def test_label_named_value_cannot_replace_the_sample() -> None:
    [series] = _parse_matrix(
        _ok("matrix", [{"metric": {"value": "FORGED"}, "values": [[1, "42"]]}])["data"]
    )
    assert series["values"][0]["value"] == 42.0
    assert series["labels"] == {"value": "FORGED"}


def test_parse_streams_merges_streams_newest_first() -> None:
    # Found in this review: 0.1.x concatenated stream groups, so "newest first" held only
    # within each stream.
    data = _streams(
        ({"s": "a"}, [(30, "a30"), (10, "a10")]),
        ({"s": "b"}, [(40, "b40"), (20, "b20")]),
    )["data"]
    assert [r["line"] for r in _parse_streams(data, "backward")] == ["b40", "a30", "b20", "a10"]
    assert [r["line"] for r in _parse_streams(data, "forward")] == ["a10", "b20", "a30", "b40"]


def test_long_line_is_cut_and_says_so(env) -> None:
    env(LOKI_MAX_LINE_CHARS="10")
    [row] = _parse_streams(_streams(({}, [(1, "x" * 25)]))["data"])
    assert row["line"] == "x" * 10
    assert row["line_truncated_chars"] == 15


def test_short_line_has_no_truncation_marker() -> None:
    [row] = _parse_streams(_streams(({}, [(1, "short")]))["data"])
    assert "line_truncated_chars" not in row


def test_ts_keeps_microseconds_of_nanosecond_stamp() -> None:
    [row] = _parse_streams(_streams(({}, [(1700000000_123456789, "x")]))["data"])
    assert row["ts"] == "2023-11-14T22:13:20.123456+00:00"


def test_parse_matrix_groups_by_series() -> None:
    data = _ok(
        "matrix",
        [{"metric": {"job": "a"}, "values": [[1700000000, "1"], [1700000060, "2"]]}],
    )["data"]
    assert _parse_matrix(data) == [
        {
            "labels": {"job": "a"},
            "values": [
                {"ts": "2023-11-14T22:13:20+00:00", "value": 1.0},
                {"ts": "2023-11-14T22:14:20+00:00", "value": 2.0},
            ],
        }
    ]


@pytest.mark.parametrize("raw", ["NaN", "+Inf", "-Inf"])
def test_non_finite_values_keep_lokis_string(raw: str) -> None:
    # FastMCP 4 serialises a non-finite float as null (measured), which reads as no data.
    [s] = _parse_vector(_ok("vector", [{"metric": {}, "value": [1, raw]}])["data"])
    assert s["value"] == raw


def test_parse_vector_scalar() -> None:
    assert _parse_vector(_ok("scalar", [1700000000, "3"])["data"]) == [
        {"labels": {}, "ts": "2023-11-14T22:13:20+00:00", "value": 3.0}
    ]


# ── Response budget ───────────────────────────────────────────────────────────


@respx.mock
async def test_response_budget_cuts_lines_and_says_so(env) -> None:
    # Hypothesis 6: 0.1.x returned up to 1000 lines of any length with no cap.
    env(LOKI_MAX_RESPONSE_CHARS="400")
    respx.get(QR).mock(
        return_value=Response(200, json=_streams(({}, [(i, "y" * 100) for i in range(20)])))
    )
    out = await query_logs('{job="x"}', start="-1h", limit=20)
    assert out["truncated"] is True
    assert 1 <= out["count"] < 20
    assert f"{20 - out['count']} of 20 lines omitted" in out["note"]
    assert "LOKI_MAX_RESPONSE_CHARS=400" in out["note"]


@respx.mock
async def test_budget_always_keeps_one_item(env) -> None:
    env(LOKI_MAX_RESPONSE_CHARS="1")
    respx.get(QR).mock(return_value=Response(200, json=_streams(({}, [(1, "a"), (2, "b")]))))
    out = await query_logs('{job="x"}', start="-1h")
    assert out["count"] == 1 and out["truncated"] is True


@respx.mock
async def test_whole_result_is_not_marked_truncated() -> None:
    respx.get(QR).mock(return_value=Response(200, json=_streams(({}, [(1, "a")]))))
    out = await query_logs('{job="x"}', start="-1h")
    assert out["truncated"] is False and "note" not in out


@pytest.mark.parametrize("var", ["LOKI_MAX_LINE_CHARS", "LOKI_MAX_RESPONSE_CHARS"])
@pytest.mark.parametrize("value", ["lots", "0"])
def test_bad_output_limit_refused_at_startup(env, var: str, value: str) -> None:
    env(**{var: value})
    with pytest.raises(server.ConfigError, match=var):
        server._validate_startup()


# ── query_logs / tail_recent ──────────────────────────────────────────────────


@respx.mock
async def test_query_logs_returns_lines_and_tenant(env) -> None:
    env(LOKI_ORG_ID="main|fake")
    route = respx.get(QR).mock(
        return_value=Response(
            200, json=_streams(({"__tenant_id__": "fake", "job": "j"}, [(1, "hello")]))
        )
    )
    out = await query_logs('{job="j"}', start="-1h")
    assert out["tenant"] == "main|fake"
    assert out["lines"][0]["labels"]["__tenant_id__"] == "fake"
    assert route.calls.last.request.headers["X-Scope-OrgID"] == "main|fake"


@respx.mock
async def test_query_logs_metric_expression_names_the_right_tool() -> None:
    # Hypothesis 4: 0.1.x returned [] and logged a warning nobody saw.
    respx.get(QR).mock(return_value=Response(200, json=_ok("matrix", [])))
    with pytest.raises(ToolError, match="use query_aggregate"):
        await query_logs('sum(rate({job="x"}[1m]))', start="-1h")


@respx.mock
async def test_query_logs_unknown_result_type() -> None:
    respx.get(QR).mock(return_value=Response(200, json={"data": {"resultType": "odd"}}))
    with pytest.raises(ToolError, match="does not handle"):
        await query_logs('{job="x"}', start="-1h")


@respx.mock
async def test_query_logs_sends_limit_and_direction() -> None:
    route = respx.get(QR).mock(return_value=Response(200, json=_streams()))
    await query_logs('{job="x"}', start="-1h", limit=7, direction="forward")
    params = route.calls.last.request.url.params
    assert params["limit"] == "7" and params["direction"] == "forward"


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 0},
        {"limit": -5},
        {"limit": 1001},
        {"direction": "sideways"},
    ],
)
async def test_query_logs_bad_arguments_rejected_by_schema(args: dict) -> None:
    # Hypothesis 5: 0.1.x sent limit<=0 and any direction to Loki.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(QR).mock(return_value=Response(200, json=_streams()))
        r = await _call("query_logs", {"logql": '{job="x"}', "start": "-1h", **args})
    assert r.is_error
    assert not route.called


@respx.mock
async def test_tail_recent_is_backward_with_tenant(env) -> None:
    env(LOKI_TENANTS="edge")
    route = respx.get(QR).mock(return_value=Response(200, json=_streams()))
    out = await tail_recent('{host="edge"}', n=20, tenant="edge")
    params = route.calls.last.request.url.params
    assert params["direction"] == "backward" and params["limit"] == "20"
    assert route.calls.last.request.headers["X-Scope-OrgID"] == "edge"
    assert out["tenant"] == "edge"


# ── query_aggregate / query_instant ───────────────────────────────────────────


@respx.mock
async def test_query_aggregate_returns_series() -> None:
    route = respx.get(QR).mock(
        return_value=Response(
            200, json=_ok("matrix", [{"metric": {"a": "b"}, "values": [[1, "5"]]}])
        )
    )
    out = await query_aggregate('count_over_time({a="b"}[1h])', start="-1h", step="1m")
    assert out["series"][0]["values"][0]["value"] == 5.0
    assert route.calls.last.request.url.params["step"] == "1m"


@respx.mock
async def test_query_aggregate_log_query_names_the_right_tool() -> None:
    respx.get(QR).mock(return_value=Response(200, json=_streams()))
    with pytest.raises(ToolError, match="use query_logs"):
        await query_aggregate('{job="x"}', start="-1h")


@respx.mock
async def test_query_instant_returns_samples() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/query").mock(
        return_value=Response(
            200, json=_ok("vector", [{"metric": {"job": "a"}, "value": [1700000000, "3"]}])
        )
    )
    out = await query_instant('sum by (job) (count_over_time({job=~".+"}[15m]))', at="-5m")
    assert out["samples"] == [
        {"labels": {"job": "a"}, "ts": "2023-11-14T22:13:20+00:00", "value": 3.0}
    ]
    sent = int(route.calls.last.request.url.params["time"])
    assert abs(sent - (time.time_ns() - 300_000_000_000)) < 2_000_000_000


@respx.mock
async def test_query_instant_scalar() -> None:
    respx.get(f"{LOKI}/loki/api/v1/query").mock(
        return_value=Response(200, json=_ok("scalar", [1700000000, "2"]))
    )
    out = await query_instant("vector(2)")
    assert out["samples"][0]["value"] == 2.0


@respx.mock
async def test_query_instant_log_query_names_the_right_tool() -> None:
    respx.get(f"{LOKI}/loki/api/v1/query").mock(return_value=Response(200, json=_streams()))
    with pytest.raises(ToolError, match="use query_logs"):
        await query_instant('{job="x"}')


# ── labels / series ───────────────────────────────────────────────────────────


@respx.mock
async def test_get_labels_sorted() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, json={"status": "success", "data": ["job", "agent"]})
    )
    out = await get_labels()
    assert out["labels"] == ["agent", "job"] and out["count"] == 2


@respx.mock
async def test_get_labels_unknown_tenant_has_no_data_key() -> None:
    # Measured: Loki answers an unknown tenant with {"status":"success"} and no "data".
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, json={"status": "success"})
    )
    assert (await get_labels())["labels"] == []


@respx.mock
async def test_get_label_values_sorted() -> None:
    respx.get(f"{LOKI}/loki/api/v1/label/job/values").mock(
        return_value=Response(200, json={"data": ["b", "a"]})
    )
    assert (await get_label_values("job"))["values"] == ["a", "b"]


@pytest.mark.parametrize("label", ["../../config", "job/x", "", "1job"])
async def test_get_label_values_rejects_bad_label(label: str) -> None:
    with pytest.raises(ToolError, match="Invalid label name"):
        await get_label_values(label)


@respx.mock
async def test_get_streams() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/series").mock(
        return_value=Response(200, json={"data": [{"job": "a"}, {"job": "b"}]})
    )
    out = await get_streams('{job=~".+"}')
    assert out["streams"] == [{"job": "a"}, {"job": "b"}]
    assert route.calls.last.request.url.params["match[]"] == '{job=~".+"}'


async def test_get_streams_rejects_non_selector() -> None:
    with pytest.raises(ToolError, match="stream selector"):
        await get_streams("job=x")


# ── get_volume ────────────────────────────────────────────────────────────────


@respx.mock
async def test_get_volume_vector_largest_first() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/index/volume").mock(
        return_value=Response(
            200,
            json=_ok(
                "vector",
                [
                    {"metric": {"job": "small"}, "value": [1, "10"]},
                    {"metric": {"job": "big"}, "value": [1, "900"]},
                ],
            ),
        )
    )
    out = await get_volume('{job=~".+"}', target_labels=["job"], aggregate_by="labels", limit=5)
    assert [v["labels"]["job"] for v in out["volumes"]] == ["big", "small"]
    assert out["volumes"][0]["bytes"] == 900.0
    params = route.calls.last.request.url.params
    assert params["targetLabels"] == "job"
    assert params["aggregateBy"] == "labels"
    assert params["limit"] == "5"


@respx.mock
async def test_get_volume_step_uses_volume_range_and_accepts_both_shapes() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/index/volume_range").mock(
        side_effect=[
            Response(200, json=_ok("matrix", [{"metric": {"job": "a"}, "values": [[1, "4"]]}])),
            # Measured on 3.7.6: a vector when the range holds one step of data.
            Response(200, json=_ok("vector", [{"metric": {"job": "a"}, "value": [1, "4"]}])),
        ]
    )
    out = await get_volume('{job="a"}', step="10m")
    assert out["series"][0]["values"][0]["value"] == 4.0
    assert route.calls.last.request.url.params["step"] == "10m"
    out = await get_volume('{job="a"}', step="10m")
    assert out["volumes"][0]["bytes"] == 4.0


async def test_get_volume_rejects_bad_target_label() -> None:
    with pytest.raises(ToolError, match="target_labels"):
        await get_volume('{job="a"}', target_labels=["ok", "not-ok"])


@respx.mock
async def test_get_volume_unexpected_shape() -> None:
    respx.get(f"{LOKI}/loki/api/v1/index/volume").mock(
        return_value=Response(200, json=_ok("streams", []))
    )
    with pytest.raises(ToolError, match="get_volume"):
        await get_volume('{job="a"}')


# ── get_stats / get_detected_fields ───────────────────────────────────────────


@respx.mock
async def test_get_stats() -> None:
    respx.get(f"{LOKI}/loki/api/v1/index/stats").mock(
        return_value=Response(200, json={"streams": 2, "chunks": 4, "entries": 12, "bytes": 99})
    )
    out = await get_stats('{job=~".+"}')
    assert (out["streams"], out["chunks"], out["entries"], out["bytes"]) == (2, 4, 12, 99)
    assert "ingesters" in out["note"]


@respx.mock
async def test_get_stats_unexpected_body() -> None:
    respx.get(f"{LOKI}/loki/api/v1/index/stats").mock(return_value=Response(200, json=[1]))
    with pytest.raises(ToolError, match="unexpected body"):
        await get_stats('{job="a"}')


@respx.mock
async def test_get_detected_fields() -> None:
    fields = [{"label": "trace_id", "type": "string", "cardinality": 3, "parsers": None}]
    route = respx.get(f"{LOKI}/loki/api/v1/detected_fields").mock(
        return_value=Response(200, json={"fields": fields, "limit": 1000})
    )
    out = await get_detected_fields('{host="edge"}', line_limit=500)
    assert out["fields"] == fields
    assert route.calls.last.request.url.params["line_limit"] == "500"


@respx.mock
async def test_get_detected_fields_none() -> None:
    respx.get(f"{LOKI}/loki/api/v1/detected_fields").mock(
        return_value=Response(200, json={"fields": None})
    )
    assert (await get_detected_fields('{host="edge"}'))["fields"] == []


# ── MCP surface ───────────────────────────────────────────────────────────────

_TOOLS = {
    "query_logs",
    "query_aggregate",
    "query_instant",
    "get_labels",
    "get_label_values",
    "get_streams",
    "get_volume",
    "get_stats",
    "get_detected_fields",
    "tail_recent",
}


async def test_tool_list_every_tool_read_only_with_tenant() -> None:
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == _TOOLS
    for t in tools.values():
        assert t.annotations is not None and t.annotations.readOnlyHint is True, t.name
        assert "tenant" in t.input_schema["properties"], t.name


@respx.mock
async def test_tool_error_reaches_client_as_error_text(env) -> None:
    env(LOKI_TENANTS="edge")
    r = await _call("get_labels", {"tenant": "main:x"})
    assert r.is_error and "contains ':'" in r.content[0].text


@respx.mock
async def test_nan_survives_serialisation_as_a_string() -> None:
    respx.get(f"{LOKI}/loki/api/v1/query").mock(
        return_value=Response(200, json=_ok("vector", [{"metric": {}, "value": [1, "NaN"]}]))
    )
    r = await _call("query_instant", {"logql": "x"})
    assert json.loads(r.content[0].text)["samples"][0]["value"] == "NaN"


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_runs_without_banner(monkeypatch) -> None:
    # The banner is what runs FastMCP's pypi.org update check.
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setenv("LOG_FILE", "")
    server.main()
    assert calls == [{"show_banner": False}]


def test_main_exits_on_bad_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda **kw: pytest.fail("must not start"))
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.setenv("LOKI_ORG_ID", "main:x")
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert "LOKI_ORG_ID" in capsys.readouterr().err


def test_iso_helper_matches_datetime() -> None:
    assert server._iso_s(0) == datetime(1970, 1, 1, tzinfo=UTC).isoformat()
