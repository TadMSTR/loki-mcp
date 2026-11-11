"""HTTP transport: configuration, bearer auth, /health and the Host/Origin guard.

The app under test is the one main() builds: main() runs with mcp.run captured, and the
captured arguments build the Starlette app through mcp.http_app. One test also starts the
real process, since a captured mcp.run proves nothing about what uvicorn serves.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from starlette.testclient import TestClient

from loki_mcp import server
from loki_mcp._client import ConfigError
from loki_mcp.server import load_http_config

TOKEN = "t" * 32
BASE = "http://127.0.0.1:8511"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


# ── configuration ─────────────────────────────────────────────────────────────


def test_no_port_is_stdio() -> None:
    assert load_http_config({}) is None
    assert load_http_config({"LOKI_MCP_PORT": "  "}) is None


def test_defaults() -> None:
    cfg = load_http_config({"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": TOKEN})
    assert cfg is not None
    assert (cfg.host, cfg.port, cfg.token) == ("127.0.0.1", 8511, TOKEN)
    assert cfg.allowed_hosts == ["localhost:8511", "127.0.0.1:8511", "[::1]:8511"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_need_no_opt_in(host: str) -> None:
    env = {"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": TOKEN, "LOKI_MCP_HOST": host}
    assert load_http_config(env).host == host


@pytest.mark.parametrize("opt_in", ["1", "true", "YES"])
def test_nonloopback_with_opt_in(opt_in: str) -> None:
    env = {
        "LOKI_MCP_PORT": "8511",
        "LOKI_MCP_API_TOKEN": TOKEN,
        "LOKI_MCP_HOST": "0.0.0.0",
        "LOKI_MCP_ALLOW_NONLOOPBACK": opt_in,
    }
    assert load_http_config(env).host == "0.0.0.0"


def test_allowed_hosts_override() -> None:
    env = {
        "LOKI_MCP_PORT": "9000",
        "LOKI_MCP_API_TOKEN": TOKEN,
        "LOKI_MCP_ALLOWED_HOSTS": " localhost:9000 , loki-mcp:9000 ,",
    }
    assert load_http_config(env).allowed_hosts == ["localhost:9000", "loki-mcp:9000"]


@pytest.mark.parametrize(
    ("env", "var"),
    [
        ({"LOKI_MCP_PORT": "http"}, "LOKI_MCP_PORT"),
        ({"LOKI_MCP_PORT": "0"}, "LOKI_MCP_PORT"),
        ({"LOKI_MCP_PORT": "70000"}, "LOKI_MCP_PORT"),
        ({"LOKI_MCP_PORT": "-1"}, "LOKI_MCP_PORT"),
        ({"LOKI_MCP_PORT": "8511"}, "LOKI_MCP_API_TOKEN: required"),
        ({"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": "x" * 15}, "too short"),
        ({"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": f" {TOKEN}"}, "whitespace"),
        (
            {"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": TOKEN, "LOKI_MCP_HOST": "0.0.0.0"},
            "LOKI_MCP_ALLOW_NONLOOPBACK=1",
        ),
        (
            {
                "LOKI_MCP_PORT": "8511",
                "LOKI_MCP_API_TOKEN": TOKEN,
                "LOKI_MCP_HOST": "0.0.0.0",
                "LOKI_MCP_ALLOW_NONLOOPBACK": "0",
            },
            "not loopback",
        ),
        (
            {"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": TOKEN, "LOKI_MCP_ALLOWED_HOSTS": "*"},
            "LOKI_MCP_ALLOWED_HOSTS",
        ),
        (
            {
                "LOKI_MCP_PORT": "8511",
                "LOKI_MCP_API_TOKEN": TOKEN,
                "LOKI_MCP_ALLOWED_HOSTS": "localhost,*.example.com",
            },
            "without wildcards",
        ),
        (
            {"LOKI_MCP_PORT": "8511", "LOKI_MCP_API_TOKEN": TOKEN, "LOKI_MCP_ALLOWED_HOSTS": ","},
            "LOKI_MCP_ALLOWED_HOSTS",
        ),
    ],
)
def test_bad_config_refused_with_the_variable_named(env: dict, var: str) -> None:
    with pytest.raises(ConfigError, match=var):
        load_http_config(env)


def test_main_bad_http_config_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.mcp, "run", lambda **kw: pytest.fail("must not start"))
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.setenv("LOKI_MCP_PORT", "8511")
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    assert "LOKI_MCP_API_TOKEN" in capsys.readouterr().err


# ── the app main() builds ─────────────────────────────────────────────────────


@pytest.fixture
def app(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.setenv("LOKI_MCP_PORT", "8511")
    monkeypatch.setenv("LOKI_MCP_API_TOKEN", TOKEN)
    server.main()
    (kw,) = calls
    assert kw["transport"] == "streamable-http" and kw["path"] == "/mcp"
    assert kw["host_origin_protection"] is True
    assert kw["show_banner"] is False
    assert (kw["host"], kw["port"]) == ("127.0.0.1", 8511)
    return server.mcp.http_app(
        path=kw["path"],
        transport=kw["transport"],
        middleware=kw["middleware"],
        host_origin_protection=kw["host_origin_protection"],
        allowed_hosts=kw["allowed_hosts"],
    )


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def test_health_needs_no_token_and_says_nothing_else(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/healthz", "/health/", "/health-debug", "/mcp", "/"])
def test_every_other_path_needs_the_token(client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        f"Basic {TOKEN}",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
        f"Bearer  {TOKEN}",
        f"Token {TOKEN}",
        TOKEN,
    ],
)
def test_wrong_or_missing_token_is_401(client, auth: str | None) -> None:
    headers = dict(MCP_HEADERS)
    if auth is not None:
        headers["authorization"] = auth
    r = client.post("/mcp", json=INIT, headers=headers)
    assert r.status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_valid_token_any_scheme_case_reaches_mcp(client, scheme: str) -> None:
    headers = {**MCP_HEADERS, "authorization": f"{scheme} {TOKEN}"}
    r = client.post("/mcp", json=INIT, headers=headers)
    assert r.status_code == 200, r.text
    assert "loki" in r.text  # serverInfo.name


# The foreign-Host check is in the real-process test below, not here: FastMCP adds
# scope["server"]'s host to the allowlist, and TestClient fills scope["server"] from its
# base_url, so a TestClient on http://evil.example is let through. Under uvicorn,
# scope["server"] is the bound address (127.0.0.1, or 0.0.0.0, which FastMCP skips).


def test_foreign_origin_refused(client) -> None:
    headers = {
        **MCP_HEADERS,
        "authorization": f"Bearer {TOKEN}",
        "origin": "https://evil.example",
    }
    assert client.post("/mcp", json=INIT, headers=headers).status_code == 403


@pytest.mark.parametrize("base", ["http://localhost:8511", "http://[::1]:8511"])
def test_loopback_host_values_allowed(app, base: str) -> None:
    with TestClient(app, base_url=base) as c:
        assert c.get("/health").status_code == 200


# ── the real process ──────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LOKI_", "OTEL_"))}
    return {**env, "LOG_FILE": "", **extra}


def test_process_without_token_exits_nonzero() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "loki_mcp.server"],
        env=_env(LOKI_MCP_PORT=str(_free_port())),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 2
    assert "LOKI_MCP_API_TOKEN" in r.stderr


def _stop(proc: subprocess.Popen) -> None:
    """Terminate, then kill. Never raises, so it can't replace the test's own failure."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def test_process_serves_health_and_refuses_mcp_without_token(tmp_path) -> None:
    # stderr goes to a file, not a pipe: nothing has to drain it while the server runs,
    # and reading it after a failed start never blocks on a live process.
    err_path = tmp_path / "stderr"
    err = err_path.open("wb")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "loki_mcp.server"],
        env=_env(
            LOKI_MCP_PORT=str(port),
            LOKI_MCP_API_TOKEN=TOKEN,
            LOKI_MCP_ALLOWED_HOSTS=f"127.0.0.1:{port}",
        ),
        stdout=subprocess.DEVNULL,
        stderr=err,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                r = httpx.get(f"{url}/health", timeout=1)
                break
            except httpx.TransportError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    _stop(proc)
                    tail = err_path.read_bytes().decode(errors="replace")[-2000:]
                    pytest.fail(f"server did not start: {tail}")
                time.sleep(0.1)
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert httpx.post(f"{url}/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        ok = httpx.post(
            f"{url}/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"bearer {TOKEN}"}
        )
        assert ok.status_code == 200, ok.text
        # DNS rebinding: a browser sends the attacker's hostname. Refused (421) even with
        # a valid token, and on /health too: the guard wraps the bearer middleware.
        evil = {"host": f"evil.example:{port}", "authorization": f"Bearer {TOKEN}"}
        assert (
            httpx.post(f"{url}/mcp", json=INIT, headers={**MCP_HEADERS, **evil}).status_code == 421
        )
        assert httpx.get(f"{url}/health", headers={"host": "evil.example"}).status_code == 421
    finally:
        _stop(proc)
        err.close()
