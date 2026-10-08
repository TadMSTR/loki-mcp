"""Tests for observability.py: log file handling and the OTEL switch."""

from __future__ import annotations

import logging
import logging.handlers
import os
import stat

import pytest

from loki_mcp import observability


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    for h in root.handlers:
        if h not in saved[0]:
            h.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


@pytest.fixture(autouse=True)
def reset_tracer(monkeypatch):
    monkeypatch.setattr(observability, "_tracer", None)
    monkeypatch.setattr(observability, "_tracer_failed", False)


def test_log_file_rotates_and_is_owner_only(tmp_path, monkeypatch) -> None:
    # Hypothesis 8: 0.1.x used a plain FileHandler (no rotation) and left the file 644.
    log_file = tmp_path / "sub" / "loki-mcp.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    observability.configure_logging()
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    assert handlers[0].maxBytes == 5 * 1024 * 1024 and handlers[0].backupCount == 3
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600


def test_existing_world_readable_log_is_tightened(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "loki-mcp.log"
    log_file.write_text("old\n")
    os.chmod(log_file, 0o644)
    monkeypatch.setenv("LOG_FILE", str(log_file))
    observability.configure_logging()
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600


def test_empty_log_file_means_stderr_only(monkeypatch) -> None:
    monkeypatch.setenv("LOG_FILE", "")
    observability.configure_logging()
    assert not any(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers)


def test_bare_filename_does_not_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_FILE", "bare.log")
    observability.configure_logging()
    assert (tmp_path / "bare.log").exists()


def test_noisy_third_party_loggers_held_at_warning(monkeypatch) -> None:
    # httpx logged every request URL (LogQL included) at INFO; the MCP SDK logged every
    # ListToolsRequest (96% of the lines in a 0.1.x log).
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    observability.configure_logging()
    for name in ("httpx", "httpcore", "mcp.server.lowlevel.server"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name


def test_tracer_off_without_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert observability.get_tracer() is None


def test_tracer_on_with_endpoint(monkeypatch) -> None:
    # The real exporter and the global provider are stubbed: a live OTLP exporter keeps a
    # background thread that logs export failures after pytest has closed stderr.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    endpoints = []
    monkeypatch.setattr(
        trace_exporter,
        "OTLPSpanExporter",
        lambda endpoint: endpoints.append(endpoint) or InMemorySpanExporter(),
    )
    monkeypatch.setattr(trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    tracer = observability.get_tracer()
    assert tracer is not None
    assert observability.get_tracer() is tracer
    assert endpoints == ["http://127.0.0.1:1"]


def test_tracer_init_failure_is_tried_once(monkeypatch) -> None:
    import builtins

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    real_import = builtins.__import__
    attempts = []

    def no_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            attempts.append(name)
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_otel)
    assert observability.get_tracer() is None
    assert observability.get_tracer() is None
    assert len(attempts) == 1
