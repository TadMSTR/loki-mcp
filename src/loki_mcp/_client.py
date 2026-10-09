"""Loki HTTP access for loki-mcp: configuration, tenant selection, one request path.

Every tool reaches Loki through ``loki_get``. That is where the tenant header is set,
where Loki's own error text is turned into a ``ToolError`` the agent can read, and
where the optional OTEL span is opened. Before 0.2.0 each tool built its own client
and called ``raise_for_status()``, so a LogQL parse error reached the agent as a bare
status code with Loki's message thrown away.

Configuration (read once, validated at startup by ``main()``):

  LOKI_URL      Loki base URL. Default ``http://localhost:3100``.
  LOKI_ORG_ID   Default ``X-Scope-OrgID``. May name several tenants joined by ``|``
                (``main|fake``). Unset: no header, which is what a single-tenant Loki
                (``auth_enabled: false``) expects.
  LOKI_TENANTS  Comma-separated allowlist for the per-call ``tenant`` argument. Each
                entry is one complete header value, so ``main|fake,edge`` allows
                exactly ``main|fake`` and ``edge``. Unset: per-call ``tenant`` is
                refused.
  LOKI_TIMEOUT  Seconds per request. Default 30.
  LOKI_MAX_BODY_BYTES
                Largest Loki response body read before it is refused. Default 32 MiB.
                The per-tool result budget (server.py) applies after parsing; this one
                bounds what is held in memory to parse at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from fastmcp.exceptions import ToolError

from .observability import get_tracer

_log = structlog.get_logger("loki-mcp")

# Loki's tenant-ID rules (docs: operations/multi-tenancy, "Restrictions"): 1-150 bytes of
# [0-9A-Za-z] and ! - _ . * ' ( ), and never exactly "." or "..".
#
# The colon is the one that matters. Measured against grafana/loki:3.7.6 with
# auth_enabled: true (2026-10-08): `X-Scope-OrgID: main:x` returns 200 with tenant
# main's data. Loki stops reading at the colon and checks nothing after it, so a
# value carrying one is not rejected by Loki; it has to be rejected here. Loki does
# reject ".", a space and a 151-byte ID itself (400), but an error from us names the
# env var or argument that caused it, which Loki's cannot.
# Used with fullmatch: `$` also matches before a trailing newline (audit F-05).
_TENANT_RE = re.compile(r"[0-9A-Za-z!\-_.*'()]{1,150}")

# Loki ignores an empty segment (`main|` reads as `main`, measured), so `main||fake`
# would quietly mean something other than what was typed. Rejected for that reason.


class ConfigError(ValueError):
    """An environment variable holds a value loki-mcp refuses to start with."""


@dataclass(frozen=True)
class Config:
    url: str
    org_id: str | None
    tenants: tuple[str, ...]
    timeout: float
    max_body_bytes: int


def validate_tenant_value(value: str, source: str) -> str:
    """Return ``value`` if it is a valid X-Scope-OrgID value, else raise ValueError.

    ``value`` may hold several tenants joined by ``|``. ``source`` names where the
    value came from so the message points at the thing to fix.
    """
    if ":" in value:
        raise ValueError(
            f"{source}: {value!r} contains ':'. Loki stops reading a tenant ID at a colon "
            "and serves the part before it, so this would read a different tenant than "
            "the one named."
        )
    parts = value.split("|")
    for part in parts:
        if part == "":
            raise ValueError(f"{source}: {value!r} has an empty tenant segment.")
        if part in (".", ".."):
            raise ValueError(f"{source}: {part!r} is not a valid tenant ID.")
        if not _TENANT_RE.fullmatch(part):
            raise ValueError(
                f"{source}: tenant {part!r} is not a valid Loki tenant ID "
                "(1-150 characters from 0-9 A-Z a-z ! - _ . * ' ( ))."
            )
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env

    url = (env.get("LOKI_URL") or "http://localhost:3100").rstrip("/")

    org_id = env.get("LOKI_ORG_ID") or None
    if org_id is not None:
        try:
            validate_tenant_value(org_id, "LOKI_ORG_ID")
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

    tenants: list[str] = []
    raw = env.get("LOKI_TENANTS") or ""
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        try:
            tenants.append(validate_tenant_value(entry, "LOKI_TENANTS"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

    raw_timeout = env.get("LOKI_TIMEOUT") or "30"
    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise ConfigError(f"LOKI_TIMEOUT: {raw_timeout!r} is not a number.") from None
    if not 0 < timeout <= 600:
        raise ConfigError(f"LOKI_TIMEOUT: {timeout} is outside (0, 600] seconds.")

    raw_body = env.get("LOKI_MAX_BODY_BYTES") or str(32 * 1024 * 1024)
    try:
        max_body = int(raw_body)
    except ValueError:
        raise ConfigError(f"LOKI_MAX_BODY_BYTES: {raw_body!r} is not an integer.") from None
    if not 1024 <= max_body <= 1024**3:
        raise ConfigError(f"LOKI_MAX_BODY_BYTES: {max_body} is outside [1024, 1 GiB].")

    return Config(
        url=url, org_id=org_id, tenants=tuple(tenants), timeout=timeout, max_body_bytes=max_body
    )


_config: Config | None = None


def config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Drop the cached config so the next ``config()`` re-reads the environment."""
    global _config
    _config = None


def resolve_tenant(tenant: str | None) -> str | None:
    """Return the X-Scope-OrgID value for a call, or None for no header.

    No ``tenant`` means ``LOKI_ORG_ID``. A ``tenant`` must be an exact entry of
    ``LOKI_TENANTS``; anything else is an error rather than a pass-through, because the
    allowlist is the only thing standing between an agent and every tenant in Loki.
    """
    cfg = config()
    if tenant is None or tenant == "":
        return cfg.org_id
    try:
        validate_tenant_value(tenant, "tenant")
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if not cfg.tenants:
        raise ToolError(
            "The tenant argument is disabled: LOKI_TENANTS is not set on this server. "
            "Omit tenant to use the default (LOKI_ORG_ID)."
        )
    if tenant not in cfg.tenants:
        raise ToolError(
            f"tenant {tenant!r} is not allowed. Allowed values: "
            + ", ".join(repr(t) for t in cfg.tenants)
        )
    return tenant


_ERROR_BODY_MAX = 500


def redact_url(url: str) -> str:
    """``url`` without any ``user:password@`` part, for messages and logs."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


class _BodyTooLarge(Exception):
    pass


def _loki_error(status: int, body: str, path: str, org: str | None) -> ToolError:
    body = body.strip()
    if status == 401 and "no org id" in body:
        return ToolError(
            "Loki refused the request with 401 'no org id': it runs with auth_enabled and "
            "this call sent no X-Scope-OrgID. Set LOKI_ORG_ID on the server (for example "
            "'main|fake') or pass tenant."
        )
    where = f" (tenant {org!r})" if org else ""
    if status >= 500:
        # A 4xx body is about the request (a LogQL parse error, a bad parameter) and the
        # agent needs it to fix the query. A 5xx body is about Loki's own state and can
        # name internal components and addresses, so it goes to the log, not the agent.
        _log.warning("loki_server_error", path=path, status=status, body=body[:2000])
        return ToolError(
            f"Loki returned {status} for {path}{where}: server-side error; details are in "
            "the loki-mcp log. Retry, or narrow the query."
        )
    if len(body) > _ERROR_BODY_MAX:
        body = body[:_ERROR_BODY_MAX] + "…"
    return ToolError(f"Loki returned {status} for {path}{where}: {body or '<empty>'}")


async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    declared = resp.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def loki_get(path: str, params: dict[str, str], org: str | None) -> Any:
    """GET ``path`` from Loki and return the parsed JSON.

    ``org`` is the X-Scope-OrgID value already returned by ``resolve_tenant``; None
    sends no header.
    """
    cfg = config()
    headers = {"X-Scope-OrgID": org} if org else {}

    tracer = get_tracer()
    span_cm = (
        tracer.start_as_current_span(
            "loki.get", attributes={"loki.path": path, "loki.tenant": org or ""}
        )
        if tracer is not None
        else contextlib.nullcontext()
    )

    started = time.monotonic()
    status: int | None = None
    try:
        with span_cm as span:
            try:
                # One client per call, on purpose. This is a stdio server that makes a
                # handful of requests per agent session to a loopback Loki over plain
                # HTTP, so a pooled client saves nothing measurable, while a module-level
                # one is bound to the first event loop that used it.
                async with (
                    httpx.AsyncClient(base_url=cfg.url, timeout=cfg.timeout) as client,
                    client.stream("GET", path, params=params, headers=headers) as resp,
                ):
                    status = resp.status_code
                    raw = await _read_capped(resp, cfg.max_body_bytes)
            except _BodyTooLarge:
                raise ToolError(
                    f"Loki's response for {path} exceeded LOKI_MAX_BODY_BYTES="
                    f"{cfg.max_body_bytes}. Narrow the time range or selector, or lower limit."
                ) from None
            except httpx.TimeoutException:
                raise ToolError(
                    f"Loki did not answer {path} within {cfg.timeout:g}s (LOKI_TIMEOUT). "
                    "Narrow the time range or the selector."
                ) from None
            except httpx.TransportError as exc:
                raise ToolError(
                    f"Cannot reach Loki at {redact_url(cfg.url)} ({type(exc).__name__})."
                ) from None
            if span is not None:
                span.set_attribute("http.status_code", status)
            if status != 200:
                raise _loki_error(status, raw.decode("utf-8", "replace"), path, org)
            try:
                return json.loads(raw)
            except ValueError:
                raise ToolError(f"Loki returned non-JSON for {path}.") from None
    finally:
        # The query text is logged at DEBUG only. At INFO a log of every LogQL string an
        # agent ran is a second, unrotated copy of the audit trail, and a line filter can
        # carry whatever the agent was searching for.
        _log.info(
            "loki_request",
            path=path,
            tenant=org,
            status=status,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        _log.debug("loki_request_params", path=path, params=params)
