"""
Observability setup for loki-mcp.

Structured logging is always on. OTEL is opt-in via env var.

  LOG_LEVEL                     default INFO
  LOG_FILE                      default /opt/appdata/loki-mcp/logs/loki-mcp.log; empty
                                disables the file and logs to stderr only
  OTEL_EXPORTER_OTLP_ENDPOINT   enables a span per Loki request (needs the [otel] extra)
"""

import logging
import logging.handlers
import os
import sys

import structlog

# The file rotates. Before 0.2.0 it was a plain FileHandler with nothing to bound it.
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3

# Third-party loggers held at WARNING. Measured in a 0.1.x log:
#   httpx                       "HTTP Request: GET <full URL>" at INFO, i.e. every LogQL
#                               query in clear, in a world-readable file
#   mcp.server.lowlevel.server  "Processing request of type ListToolsRequest", 96% of
#                               the lines
# loki-mcp logs its own one line per Loki request (see _client.loki_get).
_QUIET_LOGGERS = ("httpx", "httpcore", "mcp.server.lowlevel.server")


class _OwnerOnlyRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Every file it creates, including each one a rollover creates, is 0600.

    The stock handler opens with the process umask, so a chmod after setup covered only
    the first file: after one rollover the live log was 0644 again (measured, umask 022).
    """

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "/opt/appdata/loki-mcp/logs/loki-mcp.log")

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    stderr_handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handlers: list[logging.Handler] = [stderr_handler]
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            _OwnerOnlyRotatingFileHandler(
                log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS
            )
        )
        # A file that already existed keeps its mode through os.open, and the 0.1.x one
        # was 644, so tighten it once here too.
        os.chmod(log_file, 0o600)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for h in handlers:
        root_logger.addHandler(h)
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    for h in handlers:
        h.setFormatter(formatter)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# OTEL tracing (opt-in)
#
# Called by _client.loki_get for every Loki request. In 0.1.1 this function existed
# but nothing called it, so the "OTEL tracing opt-in" that release announced never ran.
# ---------------------------------------------------------------------------

# One dict rather than two module globals: "tracer" is the tracer once built, "failed"
# records that building it failed, so the import is not retried on every request.
_state: dict = {"tracer": None, "failed": False}


def get_tracer():
    if _state["tracer"] is not None or _state["failed"]:
        return _state["tracer"]
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "loki-mcp"})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _state["tracer"] = trace.get_tracer("loki-mcp")
    except Exception:
        # Once. Without the flag a missing [otel] extra retried the import, and logged,
        # on every request.
        _state["failed"] = True
        structlog.get_logger().warning("otel_init_failed", exc_info=True)
    return _state["tracer"]
