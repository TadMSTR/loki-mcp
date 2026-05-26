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
- SSRF via the `LOKI_URL` configuration (if an attacker can control `LOKI_URL`)
- Information disclosure through error messages or tool responses that expose internal infrastructure details
- Dependency vulnerabilities with a plausible exploitation path in loki-mcp's usage

**Out of scope:**

- Vulnerabilities in Loki itself, Grafana, or the host system
- Issues that require attacker control of the `LOKI_URL` environment variable
  (operator-controlled trust boundary, not an input attack surface)
- Theoretical weaknesses without a realistic attack path against the MCP tool surface

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
