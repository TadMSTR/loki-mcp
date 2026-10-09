"""Tests for _client.py: configuration, tenant selection and the request path."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError
from httpx import Response

from loki_mcp import _client, observability
from loki_mcp._client import ConfigError, load_config, loki_get, resolve_tenant

from .conftest import LOKI

# ── load_config ───────────────────────────────────────────────────────────────


def test_defaults() -> None:
    cfg = load_config({})
    assert cfg.url == LOKI
    assert cfg.org_id is None
    assert cfg.tenants == ()
    assert cfg.timeout == 30
    assert cfg.max_body_bytes == 32 * 1024 * 1024


def test_url_trailing_slash_stripped() -> None:
    assert load_config({"LOKI_URL": "http://loki:3100/"}).url == "http://loki:3100"


def test_multi_tenant_org_id_accepted() -> None:
    assert load_config({"LOKI_ORG_ID": "main|fake"}).org_id == "main|fake"


def test_tenants_allowlist_is_comma_separated_header_values() -> None:
    cfg = load_config({"LOKI_TENANTS": "main|fake, edge,"})
    assert cfg.tenants == ("main|fake", "edge")


@pytest.mark.parametrize(
    ("var", "value", "match"),
    [
        ("LOKI_ORG_ID", "main:x", "contains ':'"),
        ("LOKI_ORG_ID", "main||fake", "empty tenant segment"),
        ("LOKI_ORG_ID", "main|", "empty tenant segment"),
        ("LOKI_ORG_ID", "..", "not a valid tenant ID"),
        ("LOKI_ORG_ID", "a b", "not a valid Loki tenant ID"),
        ("LOKI_ORG_ID", "x" * 151, "not a valid Loki tenant ID"),
        ("LOKI_TENANTS", "edge,bad:one", "contains ':'"),
        ("LOKI_TENANTS", "edge,a/b", "not a valid Loki tenant ID"),
        ("LOKI_TIMEOUT", "soon", "not a number"),
        ("LOKI_TIMEOUT", "0", "outside"),
        ("LOKI_TIMEOUT", "601", "outside"),
        ("LOKI_MAX_BODY_BYTES", "big", "not an integer"),
        ("LOKI_MAX_BODY_BYTES", "1023", "outside"),
    ],
)
def test_invalid_config_refused(var: str, value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config({var: value})


def test_every_documented_tenant_character_accepted() -> None:
    # Loki's documented set: 0-9 a-z A-Z ! - _ . * ' ( )
    value = "Az09!-_.*'()"
    assert load_config({"LOKI_ORG_ID": value}).org_id == value
    assert load_config({"LOKI_ORG_ID": "x" * 150}).org_id == "x" * 150


# ── resolve_tenant ────────────────────────────────────────────────────────────


def test_no_tenant_means_org_id(env) -> None:
    env(LOKI_ORG_ID="main|fake")
    assert resolve_tenant(None) == "main|fake"
    assert resolve_tenant("") == "main|fake"


def test_no_tenant_and_no_org_id_means_no_header() -> None:
    assert resolve_tenant(None) is None


def test_tenant_refused_when_allowlist_unset(env) -> None:
    env(LOKI_ORG_ID="main")
    with pytest.raises(ToolError, match="LOKI_TENANTS is not set"):
        resolve_tenant("edge")


def test_tenant_on_allowlist_accepted(env) -> None:
    env(LOKI_TENANTS="main|fake,edge")
    assert resolve_tenant("edge") == "edge"
    assert resolve_tenant("main|fake") == "main|fake"


def test_tenant_not_on_allowlist_is_an_error_not_a_pass_through(env) -> None:
    env(LOKI_TENANTS="main|fake,edge")
    with pytest.raises(ToolError, match="not allowed"):
        resolve_tenant("fake")
    # A subset of an allowed multi-tenant value is not itself allowed.
    with pytest.raises(ToolError, match="not allowed"):
        resolve_tenant("main")


def test_tenant_with_colon_rejected_even_if_prefix_allowed(env) -> None:
    # Measured on Loki 3.7.6: `main:x` is served as tenant `main`.
    env(LOKI_TENANTS="main,edge")
    with pytest.raises(ToolError, match="contains ':'"):
        resolve_tenant("main:x")


def test_config_is_cached_until_reset(monkeypatch) -> None:
    monkeypatch.setenv("LOKI_ORG_ID", "one")
    _client.reset_config()
    assert _client.config().org_id == "one"
    monkeypatch.setenv("LOKI_ORG_ID", "two")
    assert _client.config().org_id == "one"
    _client.reset_config()
    assert _client.config().org_id == "two"


# ── loki_get ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_header_sent_when_org_given() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, json={"data": []})
    )
    await loki_get("/loki/api/v1/labels", {}, "main|fake")
    assert route.calls.last.request.headers["X-Scope-OrgID"] == "main|fake"


@respx.mock
async def test_no_header_when_org_none() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, json={"data": []})
    )
    await loki_get("/loki/api/v1/labels", {}, None)
    assert "X-Scope-OrgID" not in route.calls.last.request.headers


@respx.mock
async def test_loki_error_body_reaches_the_agent() -> None:
    # Hypothesis 3: 0.1.x raised a bare HTTPStatusError and lost this text.
    respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=Response(400, text="parse error at line 1, col 21: syntax error: unexpected (")
    )
    with pytest.raises(ToolError, match=r"400 .*parse error at line 1, col 21"):
        await loki_get("/loki/api/v1/query_range", {}, "main")


@respx.mock
async def test_401_no_org_id_names_the_env_var() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(401, text="no org id\n"))
    with pytest.raises(ToolError, match="LOKI_ORG_ID"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_long_error_body_is_cut() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(400, text="E" * 5000))
    with pytest.raises(ToolError) as exc:
        await loki_get("/loki/api/v1/labels", {}, None)
    assert len(str(exc.value)) < 700


@respx.mock
async def test_empty_error_body() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(404, text=""))
    with pytest.raises(ToolError, match="<empty>"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_5xx_body_goes_to_the_log_not_the_agent(monkeypatch) -> None:
    # Baseline OE-02: a 5xx body describes Loki's internals (components, addresses).
    logged = []
    monkeypatch.setattr(
        _client._log, "warning", lambda event, **kw: logged.append((event, kw)), raising=False
    )
    body = "rpc error: code = Unavailable desc = connection refused to 172.20.24.3:9095"
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(500, text=body))
    with pytest.raises(ToolError) as exc:
        await loki_get("/loki/api/v1/labels", {}, "main")
    assert "500" in str(exc.value) and "server-side error" in str(exc.value)
    assert "172.20.24.3" not in str(exc.value)
    assert logged and logged[0][0] == "loki_server_error" and "172.20.24.3" in logged[0][1]["body"]


@respx.mock
async def test_body_over_cap_refused_by_content_length(env) -> None:
    # Baseline IV-14: bound what is read before json parsing, not only what is returned.
    env(LOKI_MAX_BODY_BYTES="2048")
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, content=b"x" * 4096, headers={"content-length": "4096"})
    )
    with pytest.raises(ToolError, match="LOKI_MAX_BODY_BYTES=2048"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_body_over_cap_refused_while_streaming(env) -> None:
    # No (or a lying) Content-Length: the streamed byte count is what decides.
    env(LOKI_MAX_BODY_BYTES="2048")

    async def chunks():
        for _ in range(4):
            yield b"x" * 1024

    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(200, content=chunks()))
    with pytest.raises(ToolError, match="LOKI_MAX_BODY_BYTES"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_body_under_cap_is_parsed(env) -> None:
    env(LOKI_MAX_BODY_BYTES="2048")
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(
        return_value=Response(200, json={"data": ["a"] * 100})
    )
    assert (await loki_get("/loki/api/v1/labels", {}, None))["data"][0] == "a"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://user:s3cret@loki:3100/base", "http://loki:3100/base"),
        ("http://user@loki", "http://loki"),
        ("http://loki:3100", "http://loki:3100"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert _client.redact_url(url) == expected


@respx.mock
async def test_unreachable_error_does_not_print_url_credentials(env) -> None:
    # Baseline SC-14: LOKI_URL may carry basic-auth credentials.
    env(LOKI_URL="http://user:s3cret@loki.example:3100")
    respx.get("http://loki.example:3100/loki/api/v1/labels").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(ToolError) as exc:
        await loki_get("/loki/api/v1/labels", {}, None)
    assert "s3cret" not in str(exc.value) and "loki.example:3100" in str(exc.value)


@respx.mock
async def test_timeout_is_a_readable_error(env) -> None:
    env(LOKI_TIMEOUT="2.5")
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ToolError, match=r"within 2.5s \(LOKI_TIMEOUT\)"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_unreachable_loki_is_a_readable_error() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ToolError, match="Cannot reach Loki at http://localhost:3100"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_non_json_200_is_an_error() -> None:
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(200, text="<html>"))
    with pytest.raises(ToolError, match="non-JSON"):
        await loki_get("/loki/api/v1/labels", {}, None)


@respx.mock
async def test_timeout_setting_reaches_httpx(env, monkeypatch) -> None:
    env(LOKI_TIMEOUT="7")
    seen = {}
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_client.httpx, "AsyncClient", spy)
    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(200, json={"data": []}))
    await loki_get("/loki/api/v1/labels", {}, None)
    assert seen["timeout"] == 7.0


# ── Logging and tracing ───────────────────────────────────────────────────────


@respx.mock
async def test_query_text_not_logged_at_info(monkeypatch) -> None:
    # Hypothesis 8: in 0.1.x httpx logged every request URL, LogQL included, at INFO.
    events: list[tuple[str, dict]] = []

    class Spy:
        def info(self, event, **kw):
            events.append(("info", {"event": event, **kw}))

        def debug(self, event, **kw):
            events.append(("debug", {"event": event, **kw}))

    monkeypatch.setattr(_client, "_log", Spy())
    respx.get(f"{LOKI}/loki/api/v1/query_range").mock(return_value=Response(200, json={"data": {}}))
    await loki_get("/loki/api/v1/query_range", {"query": '{job="x"} |= "s3cr3t"'}, "main")
    info = [e for lvl, e in events if lvl == "info"]
    assert info and all("s3cr3t" not in repr(e) for e in info)
    assert info[0]["status"] == 200 and info[0]["tenant"] == "main"
    assert any("s3cr3t" in repr(e) for lvl, e in events if lvl == "debug")


@respx.mock
async def test_a_span_is_opened_per_request_when_tracing_is_on(monkeypatch) -> None:
    # Hypothesis 2: 0.1.1 announced OTEL tracing, but nothing called get_tracer().
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setitem(observability._state, "tracer", provider.get_tracer("test"))

    respx.get(f"{LOKI}/loki/api/v1/labels").mock(return_value=Response(200, json={"data": []}))
    await loki_get("/loki/api/v1/labels", {}, "edge")

    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["loki.get"]
    assert spans[0].attributes["loki.path"] == "/loki/api/v1/labels"
    assert spans[0].attributes["loki.tenant"] == "edge"
    assert spans[0].attributes["http.status_code"] == 200


@pytest.mark.parametrize("value", ["main\n", "main|fake\n", "edge\nx"])
def test_tenant_with_newline_refused(value: str) -> None:
    # Audit F-05: `$` matched before a trailing newline, so "main\n" passed load_config
    # and then failed every call as "Cannot reach Loki".
    with pytest.raises(ConfigError):
        load_config({"LOKI_ORG_ID": value})
