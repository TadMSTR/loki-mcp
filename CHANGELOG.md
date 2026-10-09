# Changelog

## [Unreleased]

## [0.2.0] — 2026-10-08

Multi-tenant Loki support, four new tools, FastMCP 4, the fleet repo standard, and a
defect review. **Every tool's output shape changed** (see Changed). Deploying it needs a
reinstall, not a restart: the package moved to `src/` and the dependency set changed.

### Added
- **Tenant support.** `LOKI_ORG_ID` is sent as `X-Scope-OrgID` on every request and may
  name several tenants (`main|fake`); results that span tenants keep Loki's
  `__tenant_id__` label. Every tool takes an optional `tenant`, which must be an exact
  entry of the `LOKI_TENANTS` allowlist; a value not on it is an error, never a
  pass-through. Tenant IDs are checked against Loki's rules, and **`:` is rejected**:
  measured on Loki 3.7.6, `X-Scope-OrgID: main:x` returns tenant `main`'s data, because
  Loki stops reading at the colon. A 401 `no org id` from Loki names `LOKI_ORG_ID`.
- **Which endpoints accept a multi-tenant header**, measured against `grafana/loki:3.7.6`
  with `auth_enabled` and `multi_tenant_queries_enabled`: `labels`, `label/<n>/values`,
  `series`, `query_range` (logs and metrics), `query`, `index/volume`,
  `index/volume_range`, `index/stats` (counts summed) and `detected_fields` all do.
  `tail` returns 400 `multiple org IDs present`, and push returns 400; this server calls
  neither. The `live-loki` CI job re-checks it on every change.
- `query_instant` (`/query`), `get_volume` (`/index/volume`, `/index/volume_range` with
  `step`), `get_stats` (`/index/stats`) and `get_detected_fields` (`/detected_fields`).
- `LOKI_TIMEOUT`, `LOKI_MAX_BODY_BYTES`, `LOKI_MAX_LINE_CHARS`, `LOKI_MAX_RESPONSE_CHARS`. Invalid configuration
  stops the server at startup with exit 2 and the variable named.
- MCP `readOnlyHint` / `idempotentHint` annotations on every tool.
- Live tests against a real multi-tenant Loki (`-m live`, CI job `live-loki`).
- Repo standard: `src/` layout, LICENSE, committed `uv.lock` (CI installs from it),
  CI (ruff, format, pytest with coverage floor on 3.11–3.14, lock-based pip-audit split
  runtime/dev, wheel check, action-pin check), CodeQL, OSSF Scorecard, gitleaks secret
  scan with a planted-secret self-test, release workflow that checks the tag against the
  package version, Dependabot (`uv` + `github-actions`, grouped, majors ignored),
  CODEOWNERS, README badge row, `.gitignore` secrets patterns.

### Changed
- **BREAKING: output shapes.** Every tool returns an object, not a bare list: the result
  under a named key (`lines`, `series`, `samples`, `labels`, `values`, `streams`,
  `volumes`, `fields`) with `count`, `tenant` and `truncated`. Log rows are
  `{ts, line, labels}` with labels **nested** (was spread into the row). Metric results
  are grouped per series, `{labels, values: [{ts, value}]}` (was one flat row per
  point).
- **BREAKING: argument validation.** `limit` and `n` must be 1–1000 (was: anything, with
  >1000 clamped), `direction` must be `backward` or `forward`, `step` must be a
  duration or seconds, selectors must start with `{`, and `start` must be before `end`.
  Errors are returned to the agent rather than sent to Loki.
- `query_logs` / `tail_recent` merge lines across streams in time order.
- A sample value of NaN / ±Inf is returned as Loki's string (`"NaN"`, `"+Inf"`).
- **FastMCP 3 → 4** (`fastmcp>=4.0.11,<5`; locked 4.1.0, MCP SDK 2.3.0). No blocker: the
  six existing tools registered with identical arguments, and the 0.1.1 suite passed
  unchanged on 4.1.0 before any other change. Bounded ranges on every dependency.
- The log file rotates (5 MB × 3) and is mode 600. `httpx`, `httpcore` and the MCP
  SDK's request logger are held at WARNING. loki-mcp logs one line per Loki request
  (path, tenant, status, duration) and the LogQL text only at DEBUG.
- Coverage floor 80 → 99 (measured 99.80%).

### Fixed — defect review
Research's nine hypotheses, each proved or refuted before fixing, plus what the review
found beyond them. "Test" names the regression test.

1. **Confirmed.** Labels spread after `ts`/`line`/`value` overwrote them. Measured on
   0.1.1: a stream label `line="FORGED"` replaced the log line, and a metric label
   `value` replaced the sample. Loki returns structured metadata merged into stream
   labels (measured), so anyone who can push chooses these names. Test:
   `test_label_named_line_or_ts_cannot_replace_the_log_line`, and the live
   `test_structured_metadata_named_line_cannot_replace_the_line`.
2. **Confirmed.** OTEL was dead code: nothing called `get_tracer()`, so the 0.1.1 entry
   below ("OTEL tracing opt-in") described a feature that never ran. Now one span per
   Loki request. Test: `test_a_span_is_opened_per_request_when_tracing_is_on`.
3. **Confirmed.** Loki's error body was lost: a LogQL syntax error is a 400 with
   `parse error at line 1, col 21: ...` (measured), and the agent got only the status.
   Test: `test_loki_error_body_reaches_the_agent`.
4. **Confirmed.** `query_logs` with a metric expression returned `[]` (Loki answers with
   a matrix, measured). Now an error naming `query_aggregate` / `query_instant`. Test:
   `test_query_logs_metric_expression_names_the_right_tool`.
5. **Confirmed.** `limit=-5`, `direction="sideways"` and `step="banana"` were all sent to
   Loki (measured on 0.1.1). Tests: `test_query_logs_bad_arguments_rejected_by_schema`,
   `test_step_rejected`.
6. **Confirmed.** No bound on response size. Now a per-line cut and a total budget, with
   `truncated` and a `note` saying what was left out. Test:
   `test_response_budget_cuts_lines_and_says_so`.
7. **Confirmed.** An ISO timestamp without an offset was local time: under
   `TZ=America/New_York`, `2026-01-01T00:00:00` was 5 h off the `Z` form (measured). Now
   UTC. Test:
   `test_parse_time_naive_iso_is_utc_not_local`.
8. **Confirmed, and wider than stated.** A 0.1.x log file (mode 644, no rotation) held
   every LogQL query in clear via httpx's INFO `HTTP Request: GET <url>` line, and 96% of
   its lines were MCP `ListToolsRequest` noise. Tests: `test_log_file_rotates_and_is_owner_only`,
   `test_noisy_third_party_loggers_held_at_warning`,
   `test_query_text_not_logged_at_info`.
9. **Partly confirmed.** `query_logs` used `build_request`/`send` while the others used
   `.get`, and timeouts were fixed at 15/30 s. Both fixed (one path, `LOKI_TIMEOUT`).
   A client per call is **kept on purpose**: stdio server, loopback Loki over plain HTTP,
   a handful of calls per session, and a module-level client binds to the first event
   loop. Test: `test_timeout_setting_reaches_httpx`.

Found beyond the list:
- `query_logs` / `tail_recent` concatenated Loki's per-stream groups, so "newest first"
  held only within a stream. Test: `test_parse_streams_merges_streams_newest_first`.
- FastMCP's startup banner ran an update check, a GET to `pypi.org` from inside the
  stdio server on every launch. Now `show_banner=False`. Test:
  `test_main_runs_without_banner`.
- FastMCP 4 serialises a non-finite float as `null` (measured), which reads as "no
  data". Test: `test_nan_survives_serialisation_as_a_string`.
- `ecosystem.config.js` described a PM2 daemon; loki-mcp is a stdio subprocess of its
  MCP client and never ran that way. Removed.
- Pre-audit security baseline:
  - the log rotated into 0644 files: the stock `RotatingFileHandler` opens with the
    umask, so a chmod after setup covered only the first file (measured, umask 022).
    Every file it creates is now opened 0600. Test: `test_rotated_files_are_owner_only_too`.
  - Loki's response body was read whole before any cap. Now streamed up to
    `LOKI_MAX_BODY_BYTES`. Tests: `test_body_over_cap_refused_*`.
  - a 5xx body (which can name Loki's internal components and addresses) reached the
    agent; it now goes to the log, and 4xx text still reaches the agent. Test:
    `test_5xx_body_goes_to_the_log_not_the_agent`.
  - `Cannot reach Loki at <url>` would have printed `user:password@` from `LOKI_URL`.
    Test: `test_unreachable_error_does_not_print_url_credentials`.
  - `.gitignore` gains `core`, `core.*`, `*.core`.
- Security audit (2026-10-08):
  - **F-01 (Medium).** On a single-tenant read Loki passes a pushed `__tenant_id__`
    through unaltered (measured, as structured metadata and as a stream label), so
    lines pushed into one tenant could claim another. loki-mcp now renames it to
    `original___tenant_id__` on single-tenant and header-less reads, as Loki does on
    multi-tenant ones, across log rows, series, samples, streams and volumes. The
    server instructions say the envelope `tenant` is authoritative. Tests:
    `test_pushed_tenant_label_renamed_on_single_tenant_reads`, live
    `test_forged_tenant_label_is_renamed_on_a_single_tenant_read`.
  - **F-02.** The response budget kept the first item whole, so one long series
    (10,802 points, 551k chars) passed a 100k budget with `truncated: false`. Its
    `values` are now cut to fit, and an item still over budget on its own is flagged.
    Tests: `test_one_long_series_is_trimmed_and_flagged`,
    `test_one_oversized_item_without_values_is_flagged`.
  - **F-04.** The release (and CI) built with an unpinned `pip install build` and a
    freshly resolved backend; now `build`, `setuptools` and `wheel` are pinned and the
    build runs `--no-isolation`. The release job no longer checks out the tree, so its
    write token is not left in `.git/config`.
  - **F-05.** The tenant, label, step and duration checks used `^…$` with `match`, which
    accepts a trailing newline; they use `fullmatch`. Tests:
    `test_tenant_with_newline_refused`, `test_label_with_newline_rejected`.
  - **F-06.** An overflowing duration (`"9"*400 + "w"`) raised a raw `OverflowError`;
    it is now a `ToolError`, and an out-of-range timestamp from Loki is returned raw
    rather than failing the result.
  - **F-08.** The instructions say log lines and label values are untrusted data.
- Security re-check round 2 (2026-10-08):
  - **R2-01.** Loki's multi-tenant `/series` adds its own `__tenant_id__` only to streams
    that lack one, so `get_streams` under `main|fake` still returned a pushed one
    (measured). On a multi-tenant header `get_streams` now asks each tenant on its own
    and sets `__tenant_id__` from the tenant it asked; a pushed one becomes
    `original___tenant_id__`. Live test:
    `test_get_streams_forged_tenant_label_under_a_multi_tenant_header`.
  - **R2-02.** Loki serves `edge|edge` as the single tenant `edge`, so a header with a
    repeated tenant looked multi-tenant to the F-01 guard while Loki added no label. A
    repeated tenant is now refused at startup, and the guard counts distinct tenants.
  - **R2-03.** The build tools install from a hash-locked
    `.github/build-requirements.txt` (transitive dependencies included), which a new
    Dependabot `pip` entry keeps current.
- `pydantic` is now imported directly (tool-argument constraints), so it is declared rather than relied on through fastmcp.

### Upgrading
- Reinstall, don't just restart: the package moved to `src/` and the dependencies
  changed.
- With multi-tenant Loki, set `LOKI_ORG_ID` (and `LOKI_TENANTS` if agents may pick a
  tenant). Loki with `auth_enabled: false` ignores the header (measured on 3.7.6), so
  both can be set before switching Loki to multi-tenant; until then every `tenant` value
  reads the one tenant there is.
- MCP clients that cache the tool list see the four new tools only after reconnecting.
- On first start an existing log file is changed to mode 600.

## [0.1.1] — 2026-05-27

### Added

- `observability.py` — structured logging always on (stderr, JSON, structlog);
  default log path `/opt/appdata/loki-mcp/logs/loki-mcp.log`; log directory
  created at startup; OTEL tracing opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`.
- `configure_logging()` wired into `main()` before `mcp.run()`.
- `[otel]` optional dep group: `opentelemetry-sdk>=1.20`,
  `opentelemetry-exporter-otlp-proto-grpc>=1.20`.
- Bare `LOG_FILE` guard: `if log_dir:` check before `os.makedirs` prevents
  `FileNotFoundError` when `LOG_FILE` is set to a bare filename.

### Security

- **`get_label_values`: label name validated before URL path interpolation** — `label`
  is now validated against `^[a-zA-Z_][a-zA-Z0-9_]*$` before being inserted into the
  Loki API path. Raises `ValueError` on invalid input. Prevents path traversal via
  `../` sequences resolved by httpx's RFC 3986 normalization.

## [0.1.0] — 2026-05-26

Initial release.

### Added
- `query_logs` — LogQL log-stream query with time range, limit, and direction
- `query_aggregate` — LogQL metric query (`count_over_time`, `rate`, `sum`, etc.)
- `get_labels` — list all label names in Loki
- `get_label_values` — list values for a specific label
- `get_streams` — list active log streams matching a selector
- `tail_recent` — convenience wrapper for most recent N log lines
- `_parse_time()` — relative duration, ISO 8601, and "now" time expression parsing
- PM2 ecosystem config for forge deployment
- 80%+ test coverage via `respx`-mocked Loki HTTP responses
