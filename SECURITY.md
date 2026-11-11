# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use one of these channels:

- **GitHub private disclosure:** Use the [Security tab](https://github.com/TadMSTR/loki-mcp/security/advisories/new) to submit a private advisory.
- **Email:** Send a description to `security.i9v75@8alias.com` with the subject line `[loki-mcp] Security Report`.

Include as much detail as possible: the affected component, steps to reproduce, and potential impact.

## Scope

**In scope:**

- LogQL injection or query manipulation that allows access to log streams beyond the agent's intended scope
- Reading a tenant that is neither in `LOKI_ORG_ID` nor on the `LOKI_TENANTS` allowlist,
  by any tool argument (the `tenant` argument, a selector, or a value Loki parses
  differently from loki-mcp, as it does with `:`)
- Log content or label names that change what a tool reports (for example, a label that
  overwrites a log line or timestamp)
- Information disclosure through error messages or tool responses that expose internal infrastructure details
- Dependency vulnerabilities with a plausible exploitation path in loki-mcp's usage
- **Any way to make `count_matches` or `compare_windows` return log line text** (through
  `selector`, `line_filter`, `by` or anything else). Both tools promise counts only; that
  promise is what they are for.
- In HTTP mode: reaching any tool without the bearer token, a path other than `/health`
  answering without it, the server starting without a token or on a non-loopback address
  without `LOKI_MCP_ALLOW_NONLOOPBACK=1`, or a DNS-rebinding request getting past the Host
  guard

**Out of scope:**

- Vulnerabilities in Loki itself, Grafana, or the host system
- Issues that require attacker control of the `LOKI_URL` environment variable
  (operator-controlled trust boundary, not an input attack surface)
- Theoretical weaknesses without a realistic attack path against the MCP tool surface

## HTTP mode (v0.3.0)

`LOKI_MCP_PORT` adds a network surface that stdio never had. What protects it:

- **Bearer token, mandatory.** `LOKI_MCP_API_TOKEN`, at least 16 characters, compared with
  `hmac.compare_digest`; the `Bearer` scheme is matched case-insensitively. Without a
  conforming token the process exits 2 at startup: there is no unauthenticated HTTP mode.
- **Fail-closed bind.** The default is `127.0.0.1`. A non-loopback address needs
  `LOKI_MCP_ALLOW_NONLOOPBACK=1`. The container image sets both `0.0.0.0` and the opt-in,
  because inside a network namespace the published port is the exposure control.
- **One unauthenticated path.** `/health`, matched exactly (`/healthz` and `/health/` need the
  token). It returns `{"status":"ok"}` and never calls Loki.
- **Host/Origin guard.** FastMCP's `host_origin_protection=True` (present in fastmcp 4.1.8, the
  locked version). A `Host` outside `LOKI_MCP_ALLOWED_HOSTS` gets 421 and a foreign `Origin`
  403, before the token is checked. Two limits, from fastmcp's source: it compares the
  **hostname only** (the port is dropped, so `localhost:8511` allows `localhost` on any port),
  and it **always allows** `127.0.0.1`, `localhost` and `::1` in addition to the list. It is a
  DNS-rebinding defence, not an access control; the token is the access control.
- **No credential in the image or the health check.** The token comes from the environment;
  the HEALTHCHECK calls `/health` without one.

## count_matches and compare_windows

These two build their LogQL from three validated parts instead of accepting a query: a bare
stream selector, a chain of line filters (`|=`, `!=`, `|~`, `!~`, each with one quoted or
backtick string), and label names for `by`. Each is a full-match grammar of what is allowed,
not a list of forbidden stages. A parser or formatting stage (`| regexp`, `| json`,
`| logfmt`, `| pattern`, `| label_format`, `| line_format`, `| unwrap`, ...) is refused,
because it can copy line text into a label, which the tool returns (measured on Loki 3.7.8:
`| regexp "(?P<x>.*)"` grouped `by (x)` returned whole lines). The labels that do come back are
stream labels and structured metadata named in `by`: label values, as `get_label_values`
returns.

## Response Expectations

| Stage | Timeline |
|-------|----------|
| Acknowledgement | Within 3 business days |
| Initial assessment | Within 7 business days |
| Fix or remediation plan | Within 30 days for critical/high; 60 days for medium/low |

This is a personal project maintained by one developer. Response times are best-effort.
If you haven't heard back within 3 business days, a follow-up email is welcome.

## Disclosure

Coordinated disclosure is preferred. Please allow time for a fix to be released before
public disclosure. The CHANGELOG documents remediated findings at an appropriate level
of detail after each release.
