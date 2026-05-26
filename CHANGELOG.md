# Changelog

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
