"""Shared fixtures: a clean loki-mcp configuration for every test."""

from __future__ import annotations

import pytest

from loki_mcp import _client

LOKI = "http://localhost:3100"

_ENV = (
    "LOKI_URL",
    "LOKI_ORG_ID",
    "LOKI_TENANTS",
    "LOKI_TIMEOUT",
    "LOKI_MAX_LINE_CHARS",
    "LOKI_MAX_RESPONSE_CHARS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts from the defaults, whatever the shell running pytest exports."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    _client.reset_config()
    yield
    _client.reset_config()


@pytest.fixture
def env(monkeypatch):
    """Set loki-mcp env vars for one test: ``env(LOKI_ORG_ID="main|fake")``."""

    def _set(**values: str) -> None:
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        _client.reset_config()

    return _set
