"""Live tests against a real multi-tenant Loki (auth_enabled + multi_tenant_queries_enabled).

Skipped unless LOKI_LIVE_URL is set. CI's `live-loki` job starts grafana/loki:3.7.6 with
tests/loki-live-config.yaml and sets it. Locally:

    docker run -d --rm --name loki-live -p 127.0.0.1:3199:3100 \
      -v "$PWD/tests/loki-live-config.yaml:/etc/loki/config.yaml:ro" \
      grafana/loki:3.7.6 -config.file=/etc/loki/config.yaml
    LOKI_LIVE_URL=http://127.0.0.1:3199 pytest -m live

Mocks cannot answer the questions these answer: which endpoints accept `a|b`, what Loki
does with a colon, and whether `__tenant_id__` really comes back.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from fastmcp.exceptions import ToolError

from loki_mcp import _client, server

LIVE = os.environ.get("LOKI_LIVE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="LOKI_LIVE_URL not set (needs a multi-tenant Loki)"),
]

MARK = f"lokimcp-live-{uuid.uuid4().hex[:8]}"


def _push(tenant: str, labels: dict, lines: list[str], sm: dict | None = None) -> None:
    now = time.time_ns()
    values = []
    for i, line in enumerate(lines):
        entry = [str(now - (len(lines) - i) * 1_000_000), line]
        if sm:
            entry.append(sm)
        values.append(entry)
    r = httpx.post(
        f"{LIVE}/loki/api/v1/push",
        headers={"X-Scope-OrgID": tenant},
        json={"streams": [{"stream": labels, "values": values}]},
        timeout=10,
    )
    assert r.status_code == 204, r.text


@pytest.fixture(scope="module", autouse=True)
def seeded():
    _push("main", {"job": "docker", "run": MARK}, [f"{MARK} main {i}" for i in range(3)])
    _push("fake", {"job": "scoped-mcp", "run": MARK}, [f"{MARK} fake {i}" for i in range(2)])
    _push(
        "edge",
        {"host": "edge", "job": "journal", "run": MARK},
        [f"{MARK} edge 0"],
        sm={"line": "FORGED-BY-SM"},
    )
    # Log queries see a push at once, but /index/volume leaves out a line until about a
    # second has passed between its timestamp and the query's `end` (measured on 3.7.6:
    # empty with end=now right after the push, present with end=now+5s, and present with
    # end=now two seconds later). Wait, bounded, with the same end=now the tools send.
    deadline = time.monotonic() + 20
    while True:
        r = httpx.get(
            f"{LIVE}/loki/api/v1/index/volume",
            headers={"X-Scope-OrgID": "main|fake"},
            params={
                "query": f'{{run="{MARK}", job=~".+"}}',
                "start": str(time.time_ns() - 3600 * 10**9),
                "end": str(time.time_ns()),
            },
            timeout=10,
        )
        if len(r.json().get("data", {}).get("result", [])) == 2:
            break
        if time.monotonic() > deadline:
            pytest.fail(f"seeded data not visible in /index/volume after 20 s: {r.text[:200]}")
        time.sleep(0.25)


@pytest.fixture
def live(env):
    def _set(**values: str) -> None:
        env(LOKI_URL=LIVE, **values)

    return _set


SEL = f'{{run="{MARK}"}}'


async def test_headerless_call_names_loki_org_id(live) -> None:
    live()
    with pytest.raises(ToolError, match="LOKI_ORG_ID"):
        await server.get_labels()


async def test_bridge_value_reads_both_tenants_with_tenant_id(live) -> None:
    live(LOKI_ORG_ID="main|fake")
    out = await server.query_logs(SEL, start="-1h")
    tenants = {row["labels"].get("__tenant_id__") for row in out["lines"]}
    assert tenants == {"main", "fake"}
    assert out["count"] == 5


async def test_single_tenant_has_no_tenant_label(live) -> None:
    live(LOKI_ORG_ID="main")
    out = await server.query_logs(SEL, start="-1h")
    assert out["count"] == 3
    assert all("__tenant_id__" not in row["labels"] for row in out["lines"])


async def test_tenant_argus_reads_only_argus(live) -> None:
    live(LOKI_ORG_ID="main|fake", LOKI_TENANTS="main|fake,edge")
    out = await server.query_logs(SEL, start="-1h", tenant="edge")
    assert out["count"] == 1
    assert out["lines"][0]["line"] == f"{MARK} edge 0"
    assert out["tenant"] == "edge"


async def test_structured_metadata_named_line_cannot_replace_the_line(live) -> None:
    # Hypothesis 1, against real Loki: SM comes back merged into the stream labels.
    live(LOKI_TENANTS="edge")
    out = await server.query_logs(SEL, start="-1h", tenant="edge")
    row = out["lines"][0]
    assert row["line"] == f"{MARK} edge 0"
    assert row["labels"]["line"] == "FORGED-BY-SM"


async def test_tenant_off_the_allowlist_rejected_before_any_request(live) -> None:
    live(LOKI_ORG_ID="main", LOKI_TENANTS="main|fake")
    with pytest.raises(ToolError, match="not allowed"):
        await server.get_labels(tenant="edge")


async def test_colon_rejected_although_loki_would_serve_it(live) -> None:
    # The reason the client checks: Loki answers `main:x` with tenant main's data.
    r = httpx.get(
        f"{LIVE}/loki/api/v1/label/run/values", headers={"X-Scope-OrgID": "main:x"}, timeout=10
    )
    assert r.status_code == 200 and MARK in r.json().get("data", [])
    live(LOKI_TENANTS="main")
    with pytest.raises(ToolError, match="contains ':'"):
        await server.get_labels(tenant="main:x")


@pytest.mark.parametrize(
    ("tool", "kwargs", "key"),
    [
        ("get_labels", {}, "labels"),
        ("get_label_values", {"label": "job"}, "values"),
        ("get_streams", {"selector": SEL}, "streams"),
        ("query_logs", {"logql": SEL, "start": "-1h"}, "lines"),
        (
            "query_aggregate",
            {"logql": f"sum by (job) (count_over_time({SEL}[5m]))", "start": "-1h", "step": "1m"},
            "series",
        ),
        ("query_instant", {"logql": f"sum by (job) (count_over_time({SEL}[1h]))"}, "samples"),
        ("get_volume", {"selector": SEL}, "volumes"),
        ("get_detected_fields", {"selector": SEL}, "fields"),
    ],
)
async def test_every_list_tool_accepts_a_multi_tenant_header(live, tool, kwargs, key) -> None:
    # Measured 2026-10-08: every read endpoint loki-mcp calls accepts `a|b`. Only /tail
    # (not called) and push reject it. This test keeps that measurement current.
    live(LOKI_ORG_ID="main|fake")
    out = await getattr(server, tool)(**kwargs)
    assert out["tenant"] == "main|fake"
    assert out[key], f"{tool} returned nothing for main|fake"


async def test_query_instant_counts_across_tenants(live) -> None:
    live(LOKI_ORG_ID="main|fake")
    out = await server.query_instant(f"sum by (job) (count_over_time({SEL}[1h]))")
    counts = {s["labels"]["job"]: s["value"] for s in out["samples"]}
    assert counts == {"docker": 3.0, "scoped-mcp": 2.0}


async def test_get_stats_accepts_a_multi_tenant_header(live) -> None:
    # Values may be 0 until chunks flush (index only), so only the call is asserted.
    live(LOKI_ORG_ID="main|fake")
    out = await server.get_stats(SEL)
    assert out["tenant"] == "main|fake"
    assert set(out) >= {"streams", "chunks", "entries", "bytes"}


async def test_logql_error_text_reaches_the_agent(live) -> None:
    live(LOKI_ORG_ID="main")
    with pytest.raises(ToolError, match="parse error"):
        await server.query_logs(f"{SEL} | bad(", start="-1h")


def test_config_reset_after_live_tests() -> None:
    _client.reset_config()
    assert _client.config().url == "http://localhost:3100"
