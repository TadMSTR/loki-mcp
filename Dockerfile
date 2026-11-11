# syntax=docker/dockerfile:1

# python:3.13-slim, pinned by multi-arch index digest rather than by tag: the tag moves.
# Re-resolve with `docker buildx imagetools inspect python:3.13-slim` when bumping
# (resolved 2026-11-11). Do not copy a digest from a sibling repo; it is pinned to a
# different snapshot. Dependabot's docker ecosystem keeps both FROM digests current.
#
# slim rather than alpine: under the [otel] extra the tree includes grpcio, which
# publishes manylinux wheels; on musl it can turn into a build toolchain in the image.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# uv installs exactly uv.lock (--frozen), the set CI tests and pip-audit checks. A
# `pip install '.[otel]'` here would re-resolve the ranges in pyproject.toml at build
# time, so the image could ship versions no gate ever saw.
FROM ghcr.io/astral-sh/uv:0.11.18@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /src
# Dependencies first, without the project, so a source edit does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra otel
# [otel] is in that set deliberately: setting OTEL_EXPORTER_OTLP_ENDPOINT without it does
# not fail loudly, the exporter import fails once and nothing is ever exported
# (vikunja#336).

# The build backend, hash-pinned. uv.lock does not lock build dependencies, so letting
# uv build the project would fetch whatever setuptools satisfies `>=68` that day. These are
# the same hashed tools release.yml builds the wheel with (.github/build-requirements.txt,
# kept current by Dependabot's pip ecosystem), in their own venv so none of them ships.
COPY .github/build-requirements.txt ./build-requirements.txt
RUN python -m venv /opt/buildtools \
 && /opt/buildtools/bin/pip install --no-cache-dir --require-hashes -r build-requirements.txt

# Then the project itself: a wheel built without isolation by those tools, installed into
# /opt/venv with --no-deps (the dependencies are already exactly the lock's). The runtime
# stage takes /opt/venv and nothing from /src or /opt/buildtools.
COPY README.md LICENSE ./
COPY src ./src
RUN /opt/buildtools/bin/python -m build --no-isolation --wheel --outdir /dist . \
 && uv pip install --python /opt/venv/bin/python --no-deps /dist/*.whl

FROM ${PYTHON_IMAGE} AS runtime

# LOKI_MCP_HOST=0.0.0.0 is what makes the server reachable from outside its own network
# namespace, and is not an exposure decision: inside a namespace the bind address is not
# the security control, the compose `ports:` publish is (deploy/ publishes loopback only).
# Do not "harden" it back to 127.0.0.1: that is a container that reports healthy while
# every agent call fails (vikunja#462).
#
# LOKI_MCP_ALLOW_NONLOOPBACK=1 is not redundant with it. main() refuses a non-loopback
# bind without this opt-in, so widening the bind is a choice someone made, not a side
# effect of one variable. It is baked because running in a namespace is inherent to this
# image.
#
# LOKI_MCP_PORT=8511 makes the image serve HTTP by default. To use it as a stdio server
# (`docker run -i`), pass `-e LOKI_MCP_PORT=`.
#
# LOG_FILE= (empty): logs go to stderr only, which is what makes read_only: true work with
# no writable path. The non-image default is a file under /opt/appdata/loki-mcp/logs.
#
# Deliberately absent:
#   LOKI_URL            its default, http://localhost:3100, is the container's OWN
#                       loopback, where nothing listens. The container still starts,
#                       reports healthy and answers /health, and every tool fails. It must
#                       come from compose (http://loki:3100 on the stack network).
#   LOKI_MCP_API_TOKEN  never bake a credential into a layer. main() refuses to start
#                       without one of at least 16 characters.
ENV LOKI_MCP_HOST=0.0.0.0 \
    LOKI_MCP_ALLOW_NONLOOPBACK=1 \
    LOKI_MCP_PORT=8511 \
    LOG_FILE= \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /opt/venv /opt/venv

# Non-root, and not uid 1000: on a typical single-user host that is the operator's login
# account, so a container escape would land with its files and groups (KB DC-15). 65534
# (nobody/nogroup) owns nothing, exists in the base image, and is what grafana-mcp runs as.
USER 65534:65534

EXPOSE 8511

# /health is unauthenticated by design (exact-path exemption in _BearerAuthMiddleware) and
# returns a bare status, so the check carries no token: a HEALTHCHECK with the bearer in
# it would put the credential in every `docker inspect`. It is liveness only and never
# calls Loki, so a Loki restart does not get this working container restarted. It sends
# Host 127.0.0.1, which FastMCP's Host guard always allows.
#
# python rather than wget/curl: slim ships neither.
# Exec form (hadolint DL3025): no shell in the path. A failed urlopen raises, and python
# exits 1 on an uncaught exception, which is what HEALTHCHECK reads as unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('LOKI_MCP_PORT','8511')+'/health', timeout=4).status==200 else 1)"]

CMD ["loki-mcp"]
