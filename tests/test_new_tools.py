"""Tests for the v0.3.0 tools: get_health, get_detected_labels, get_patterns,
count_matches and compare_windows."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

from loki_mcp import server
from loki_mcp.server import (
    compare_windows,
    count_matches,
    get_detected_labels,
    get_health,
    get_patterns,
    mcp,
)

from .conftest import LOKI

Q = f"{LOKI}/loki/api/v1/query"
PATTERNS = f"{LOKI}/loki/api/v1/patterns"


def _vector(*rows: tuple[dict, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1794415200, v]} for m, v in rows],
        },
    }


def _query_of(route: respx.Route, call: int = -1) -> dict[str, str]:
    qs = parse_qs(urlsplit(str(route.calls[call].request.url)).query)
    return {k: v[0] for k, v in qs.items()}


# ── get_health ────────────────────────────────────────────────────────────────

BUILDINFO = {"version": "3.7.8", "revision": "09e6ce2f", "branch": "release-3.7.x"}


@respx.mock
async def test_get_health_ready(env) -> None:
    env(LOKI_ORG_ID="main|fake")
    respx.get(f"{LOKI}/ready").mock(return_value=Response(200, text="ready\n"))
    respx.get(f"{LOKI}/loki/api/v1/status/buildinfo").mock(
        return_value=Response(200, json=BUILDINFO)
    )
    assert await get_health() == {
        "ready": True,
        "reason": None,
        "version": "3.7.8",
        "revision": "09e6ce2f",
        "tenant": "main|fake",
    }


@respx.mock
async def test_get_health_not_ready_is_an_answer_not_an_error() -> None:
    respx.get(f"{LOKI}/ready").mock(return_value=Response(503, text="Ingester not ready"))
    respx.get(f"{LOKI}/loki/api/v1/status/buildinfo").mock(
        return_value=Response(200, json=BUILDINFO)
    )
    out = await get_health()
    assert out["ready"] is False
    assert "503" in out["reason"]
    assert out["version"] == "3.7.8"


@respx.mock
async def test_get_health_unreachable() -> None:
    respx.get(f"{LOKI}/ready").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{LOKI}/loki/api/v1/status/buildinfo").mock(
        side_effect=httpx.ConnectError("refused")
    )
    out = await get_health()
    assert out["ready"] is False and "Cannot reach Loki" in out["reason"]
    assert out["version"] is None and out["revision"] is None


@respx.mock
async def test_get_health_200_without_ready_word() -> None:
    respx.get(f"{LOKI}/ready").mock(return_value=Response(200, text="something else"))
    respx.get(f"{LOKI}/loki/api/v1/status/buildinfo").mock(return_value=Response(200, json=[]))
    out = await get_health()
    assert out["ready"] is False and "something else" in out["reason"]
    assert out["version"] is None


@respx.mock
async def test_get_health_buildinfo_failure_is_the_reason_when_ready() -> None:
    respx.get(f"{LOKI}/ready").mock(return_value=Response(200, text="ready"))
    respx.get(f"{LOKI}/loki/api/v1/status/buildinfo").mock(return_value=Response(404))
    out = await get_health()
    assert out["ready"] is True and out["reason"].startswith("buildinfo:")


async def test_get_health_unknown_tenant_refused_before_any_request(env) -> None:
    env(LOKI_TENANTS="edge")
    with pytest.raises(ToolError, match="not allowed"):
        await get_health(tenant="main")


# ── get_detected_labels ───────────────────────────────────────────────────────


@respx.mock
async def test_get_detected_labels_highest_cardinality_first(env) -> None:
    env(LOKI_ORG_ID="main")
    route = respx.get(f"{LOKI}/loki/api/v1/detected_labels").mock(
        return_value=Response(
            200,
            json={
                "detectedLabels": [
                    {"label": "job", "cardinality": 3},
                    {"label": "container", "cardinality": 41},
                    {"label": "odd", "cardinality": "x"},
                    "not-a-dict",
                ]
            },
        )
    )
    out = await get_detected_labels('{host="web01"}')
    assert [r["label"] for r in out["labels"]] == ["container", "job", "odd"]
    assert out["count"] == 3 and out["tenant"] == "main" and out["truncated"] is False
    assert _query_of(route)["query"] == '{host="web01"}'


@respx.mock
async def test_get_detected_labels_wrong_shape_is_empty() -> None:
    respx.get(f"{LOKI}/loki/api/v1/detected_labels").mock(return_value=Response(200, json=[]))
    assert (await get_detected_labels())["labels"] == []


async def test_get_detected_labels_rejects_non_selector() -> None:
    with pytest.raises(ToolError, match="stream selector"):
        await get_detected_labels("job")


# ── get_patterns ──────────────────────────────────────────────────────────────


def _patterns(*rows: tuple[str, list]) -> dict:
    return {
        "status": "success",
        "data": [{"pattern": p, "level": "info", "samples": s} for p, s in rows],
    }


@respx.mock
async def test_get_patterns_sorted_by_total_without_samples() -> None:
    route = respx.get(PATTERNS).mock(
        return_value=Response(
            200,
            json=_patterns(
                ("rare <_>", [[1794415200, 2]]),
                ("common <_>", [[1794415200, 60], [1794415260, 40]]),
            ),
        )
    )
    out = await get_patterns('{job="docker"}')
    assert out["patterns"] == [
        {"pattern": "common <_>", "level": "info", "total": 100},
        {"pattern": "rare <_>", "level": "info", "total": 2},
    ]
    assert "step" not in _query_of(route)


@respx.mock
async def test_get_patterns_samples_only_with_step() -> None:
    route = respx.get(PATTERNS).mock(
        return_value=Response(200, json=_patterns(("p <_>", [[1794415200, 5]])))
    )
    out = await get_patterns('{job="docker"}', step="1m")
    assert out["patterns"][0]["samples"] == [{"ts": "2026-11-11T16:40:00+00:00", "count": 5}]
    assert _query_of(route)["step"] == "1m"


@respx.mock
async def test_get_patterns_text_is_cut_like_a_log_line(env) -> None:
    env(LOKI_MAX_LINE_CHARS="10")
    respx.get(PATTERNS).mock(
        return_value=Response(200, json=_patterns(("x" * 25, [[1794415200, 1]])))
    )
    row = (await get_patterns('{job="docker"}'))["patterns"][0]
    assert row["pattern"] == "x" * 10 and row["pattern_truncated_chars"] == 15


@respx.mock
async def test_get_patterns_limit_says_what_it_dropped() -> None:
    rows = [(f"p{i} <_>", [[1794415200, i + 1]]) for i in range(5)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(*rows)))
    out = await get_patterns('{job="docker"}', limit=2)
    assert [p["total"] for p in out["patterns"]] == [5, 4]
    assert out["truncated"] is True and "3 less frequent patterns" in out["note"]


@respx.mock
async def test_get_patterns_limit_and_budget_notes_combine(env) -> None:
    env(LOKI_MAX_RESPONSE_CHARS="80")
    rows = [(f"pattern-{i}-" + "y" * 30, [[1794415200, 10 - i]]) for i in range(5)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(*rows)))
    out = await get_patterns('{job="docker"}', limit=3)
    assert out["count"] == 1
    assert "LOKI_MAX_RESPONSE_CHARS" in out["note"] and "beyond limit=3" in out["note"]


# F-02 probe: every place get_patterns turns Loki's sample lists into objects, crossed
# with the shapes that reach it (one huge pattern, many patterns at the top limit, a
# list cut by the budget). Each fails on the pre-fix code, which expanded every sample
# before limit and left `samples` out of the budget cut.


def _count_iso(monkeypatch) -> list[int]:
    calls = [0]
    real = server._iso_s

    def counting(ts):
        calls[0] += 1
        return real(ts)

    monkeypatch.setattr(server, "_iso_s", counting)
    return calls


@respx.mock
async def test_get_patterns_one_huge_sample_list_is_cut_to_the_budget(env, monkeypatch) -> None:
    env(LOKI_MAX_RESPONSE_CHARS="2000")
    samples = [[1794415200 + 60 * i, 1] for i in range(50_000)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(("p <_>", samples))))
    calls = _count_iso(monkeypatch)
    out = await get_patterns('{job="docker"}', step="1m")
    row = out["patterns"][0]
    assert row["total"] == 50_000  # counted from every sample, built from few
    assert 0 < len(row["samples"]) < 100
    assert calls[0] <= 2000 // 40 + 2
    assert row["samples_omitted"] > 0
    assert out["truncated"] is True
    assert len(json.dumps(out["patterns"])) <= 2000 + 200


@respx.mock
async def test_get_patterns_samples_across_many_patterns_are_bounded(env, monkeypatch) -> None:
    env(LOKI_MAX_RESPONSE_CHARS="5000")
    rows = [(f"p{i} <_>", [[1794415200 + 60 * j, 1] for j in range(500)]) for i in range(1000)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(*rows)))
    calls = _count_iso(monkeypatch)
    out = await get_patterns('{job="docker"}', step="1m", limit=1000)
    assert calls[0] <= 5000 // 40 + 2  # not 1000 x 500
    assert out["truncated"] is True
    assert len(json.dumps(out["patterns"])) <= 5000 + 200


@respx.mock
async def test_get_patterns_first_item_samples_are_trimmed_and_noted(env) -> None:
    # The first row is always kept, so its samples are what _shrink_values must cut.
    env(LOKI_MAX_RESPONSE_CHARS="600")
    samples = [[1794415200 + 60 * i, 1] for i in range(40)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(("p <_>", samples))))
    out = await get_patterns('{job="docker"}', step="1m")
    assert out["count"] == 1 and out["truncated"] is True
    assert "points of the first item omitted" in out["note"]
    assert "larger than the budget" not in out["note"]
    assert len(json.dumps(out["patterns"])) <= 600


@respx.mock
async def test_get_patterns_non_numeric_sample_ts_does_not_abort_the_call() -> None:
    # A sample whose timestamp isn't a number is relayed as short text, not a TypeError.
    samples = [[[1, 2], 3], [{"a": "b" * 500}, 4], [1794415200, 5]]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(("p <_>", samples))))
    out = await get_patterns('{job="docker"}', step="1m")
    ts = [s["ts"] for s in out["patterns"][0]["samples"]]
    assert ts[0] == "[1, 2]" and len(ts[1]) == 64 and ts[2].startswith("2026-11-11T")


@respx.mock
async def test_get_patterns_bad_sample_items_never_abort_either_loop() -> None:
    # OV-33 probe over both per-sample loops (total, and the sample rows): every bad
    # shape a sample can take is skipped or relayed, and the good ones still count.
    samples = [
        [1794415200, float("inf")],  # sent as Infinity; int() raises OverflowError
        [1794415200, float("nan")],
        [1794415200, None],
        [1794415200, "x"],
        [1794415200, [1]],
        [[1], 2],
        [{"k": "v"}, 3],
        [None, 4],
        [1794415200, 5],
    ]
    # Raw body: httpx's json= refuses non-finite floats, but a server can send them.
    body = json.dumps(_patterns(("p <_>", samples)), allow_nan=True)
    respx.get(PATTERNS).mock(
        return_value=Response(200, content=body, headers={"content-type": "application/json"})
    )
    out = await get_patterns('{job="docker"}', step="1m")
    row = out["patterns"][0]
    assert row["total"] == 2 + 3 + 4 + 5
    assert len(row["samples"]) == len(samples)


@respx.mock
async def test_get_patterns_without_step_builds_no_samples(monkeypatch) -> None:
    samples = [[1794415200 + 60 * i, 1] for i in range(10_000)]
    respx.get(PATTERNS).mock(return_value=Response(200, json=_patterns(("p <_>", samples))))
    calls = _count_iso(monkeypatch)
    out = await get_patterns('{job="docker"}')
    assert calls[0] == 0 and "samples" not in out["patterns"][0]


@respx.mock
async def test_get_patterns_skips_malformed_rows_and_samples() -> None:
    body = {
        "status": "success",
        "data": [
            "junk",
            {"pattern": 5},
            {"pattern": "ok <_>", "samples": [[1794415200, "7"], [1], [1794415260, "x"], "s"]},
        ],
    }
    respx.get(PATTERNS).mock(return_value=Response(200, json=body))
    out = await get_patterns('{job="docker"}')
    assert out["patterns"] == [{"pattern": "ok <_>", "level": None, "total": 7}]


@respx.mock
async def test_get_patterns_ingester_off_names_the_setting() -> None:
    # Measured on Loki 3.7.8: 404 with an empty body when the pattern ingester is off.
    respx.get(PATTERNS).mock(return_value=Response(404, text=""))
    with pytest.raises(ToolError, match=r"pattern_ingester\.enabled"):
        await get_patterns('{job="docker"}')


@respx.mock
async def test_other_404_keeps_lokis_message() -> None:
    respx.get(f"{LOKI}/loki/api/v1/detected_labels").mock(
        return_value=Response(404, text="404 page not found")
    )
    with pytest.raises(ToolError, match="404 page not found"):
        await get_detected_labels()


# ── count_matches ─────────────────────────────────────────────────────────────


@respx.mock
async def test_count_matches_builds_the_query_and_returns_counts_only(env) -> None:
    env(LOKI_ORG_ID="main")
    route = respx.get(Q).mock(
        return_value=Response(
            200,
            json=_vector(({"host": "web01"}, "12"), ({"host": "db01"}, "30")),
        )
    )
    out = await count_matches(
        '{job=~".+"}', line_filter='|~ "(?i)token"', start="-1h", end="now", by="host"
    )
    assert out["groups"] == [
        {"labels": {"host": "db01"}, "count": 30},
        {"labels": {"host": "web01"}, "count": 12},
    ]
    assert out["total"] == 42 and out["tenant"] == "main"
    q = _query_of(route)
    assert q["query"] == 'sum by (host) (count_over_time({job=~".+"} |~ "(?i)token" [3600s]))'
    assert q["query"] == out["query"]
    assert "line" not in json.dumps(out["groups"])


@respx.mock
async def test_count_matches_keeps_only_by_labels() -> None:
    # sum by (host) drops everything else; the tool keeps that true regardless.
    respx.get(Q).mock(
        return_value=Response(200, json=_vector(({"host": "a", "x": "LEAKED LINE"}, "1")))
    )
    out = await count_matches('{job="a"}', by="host")
    assert out["groups"] == [{"labels": {"host": "a"}, "count": 1}]


@respx.mock
async def test_count_matches_multi_tenant_keeps_lokis_tenant_label(env) -> None:
    env(LOKI_ORG_ID="main|edge")
    respx.get(Q).mock(
        return_value=Response(
            200,
            json=_vector(({"host": "a", "__tenant_id__": "edge"}, "3")),
        )
    )
    out = await count_matches('{job="a"}', by="host")
    assert out["groups"][0]["labels"] == {"host": "a", "__tenant_id__": "edge"}


@respx.mock
async def test_count_matches_no_match_is_empty_with_zero_total() -> None:
    respx.get(Q).mock(return_value=Response(200, json=_vector()))
    out = await count_matches('{job="a"}', '|= "nothing"')
    assert out["groups"] == [] and out["total"] == 0 and out["count"] == 0


@respx.mock
async def test_count_matches_non_finite_value_kept_as_string() -> None:
    respx.get(Q).mock(return_value=Response(200, json=_vector(({}, "NaN"), ({}, "2"))))
    out = await count_matches('{job="a"}')
    assert out["groups"] == [{"labels": {}, "count": 2}, {"labels": {}, "count": "NaN"}]
    assert out["total"] == 2


@respx.mock
async def test_count_matches_wrong_shape() -> None:
    respx.get(Q).mock(
        return_value=Response(200, json={"data": {"resultType": "streams", "result": []}})
    )
    with pytest.raises(ToolError, match="resultType 'streams'"):
        await count_matches('{job="a"}')


@respx.mock
async def test_count_matches_budget_truncates_and_says_so(env) -> None:
    env(LOKI_MAX_RESPONSE_CHARS="100")
    rows = [({"host": f"h{i:03d}"}, str(100 - i)) for i in range(20)]
    respx.get(Q).mock(return_value=Response(200, json=_vector(*rows)))
    out = await count_matches('{job="a"}', by="host")
    assert out["truncated"] is True and out["count"] < 20
    assert out["groups"][0]["count"] == 100


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"selector": '{job="a"} | json'}, "stream selector"),
        ({"selector": '{job="a"}', "line_filter": '| regexp "(?P<x>.*)"'}, "only line filters"),
        ({"selector": '{job="a"}', "by": "x) or vector(1"}, "label names"),
        ({"selector": '{job="a"}', "start": "-31d"}, "720h"),
        ({"selector": '{job="a"}', "start": "now", "end": "-1h"}, "must be before"),
    ],
)
async def test_count_matches_refuses_before_any_request(kwargs: dict, match: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(Q)
        with pytest.raises(ToolError, match=match):
            await count_matches(**kwargs)
        assert not route.called


# ── compare_windows ───────────────────────────────────────────────────────────


@respx.mock
async def test_compare_windows_joins_both_sides() -> None:
    route = respx.get(Q).mock(
        side_effect=[
            Response(
                200,
                json=_vector(({"host": "a"}, "10"), ({"host": "gone"}, "4"), ({"host": "z"}, "5")),
            ),
            Response(
                200,
                json=_vector(({"host": "a"}, "25"), ({"host": "new"}, "7"), ({"host": "z"}, "5")),
            ),
        ]
    )
    out = await compare_windows('{job="a"}', line_filter='|= "error"', by="host")
    assert out["groups"] == [
        {"labels": {"host": "a"}, "before": 10, "after": 25, "delta": 15, "ratio": 2.5},
        {"labels": {"host": "new"}, "before": 0, "after": 7, "delta": 7, "ratio": None},
        {"labels": {"host": "gone"}, "before": 4, "after": 0, "delta": -4, "ratio": 0.0},
        {"labels": {"host": "z"}, "before": 5, "after": 5, "delta": 0, "ratio": 1.0},
    ]
    first, second = _query_of(route, 0), _query_of(route, 1)
    # Default windows are 24h each: [-48h, -24h) evaluated at -24h, then [-24h, now).
    assert "[86400s]" in first["query"] and "[86400s]" in second["query"]
    assert int(first["time"]) < int(second["time"])
    assert set(out) >= {"window_a", "window_b", "count", "tenant", "truncated"}


@respx.mock
async def test_compare_windows_non_finite_counts_as_zero() -> None:
    respx.get(Q).mock(
        side_effect=[
            Response(200, json=_vector(({}, "NaN"))),
            Response(200, json=_vector(({}, "3"))),
        ]
    )
    out = await compare_windows('{job="a"}')
    assert out["groups"] == [{"labels": {}, "before": 0, "after": 3, "delta": 3, "ratio": None}]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"window_a": "-24h", "window_b": "-48h"}, "window_a < window_b < now"),
        ({"window_a": "now", "window_b": "-1h"}, "window_a < window_b < now"),
        # Parsed microseconds apart, so ordered; refused as an empty window instead.
        ({"window_a": "-1h", "window_b": "-1h"}, "at least one second"),
        ({"window_a": "-1h", "window_b": "now"}, "at least one second"),
    ],
)
async def test_compare_windows_order_enforced_before_any_request(kwargs: dict, match: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(Q)
        with pytest.raises(ToolError, match=match):
            await compare_windows('{job="a"}', **kwargs)
        assert not route.called


async def test_compare_windows_uses_the_same_validator() -> None:
    with pytest.raises(ToolError, match="only line filters"):
        await compare_windows('{job="a"}', line_filter="| logfmt")
    with pytest.raises(ToolError, match="stream selector"):
        await compare_windows('{job="a"} | logfmt')


async def test_compare_windows_window_past_lookback_refused() -> None:
    with pytest.raises(ToolError, match="720h"):
        await compare_windows('{job="a"}', window_a="-60d", window_b="-1d")


# ── MCP surface ───────────────────────────────────────────────────────────────


@respx.mock
async def test_count_matches_through_the_client_has_no_line_key() -> None:
    respx.get(Q).mock(return_value=Response(200, json=_vector(({"host": "a"}, "1"))))
    async with Client(mcp) as c:
        r = await c.call_tool("count_matches", {"selector": '{job="a"}', "by": "host"})
    body = json.loads(r.content[0].text)
    assert body["groups"] == [{"labels": {"host": "a"}, "count": 1}]
    assert "line" not in body


async def test_tools_list_has_fifteen_tools() -> None:
    async with Client(mcp) as c:
        assert len(await c.list_tools()) == 15


def test_server_docstring_lists_every_tool() -> None:
    for name in ("get_health", "get_detected_labels", "get_patterns", "count_matches"):
        assert name in server.__doc__
    assert "compare_windows" in server.__doc__
