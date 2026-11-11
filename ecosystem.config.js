// PM2 example: loki-mcp as a long-running HTTP server on the host.
//
// An example. For a container deployment see deploy/docker-compose.loki-mcp.yml.
// Adjust the paths. Install from the lock first, so the venv holds the versions CI audits:
//   uv sync --frozen --no-dev --extra otel
//
// The token does not belong in this file. pm2 start captures LOKI_MCP_API_TOKEN from
// the environment it runs in: source a mode-600 env file in a subshell for that one
// command (README, PM2). If the same command starts the PM2 daemon, the daemon keeps the
// token and every app it starts later inherits it, so start the daemon first from a shell
// without it. The process refuses to start in HTTP mode without a token of at least 16
// characters.
module.exports = {
  apps: [
    {
      name: "loki-mcp",
      script: ".venv/bin/python",
      args: "-m loki_mcp.server",
      cwd: __dirname,
      interpreter: "none",

      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "10s",

      env: {
        LOKI_URL: "http://localhost:3100",
        // Multi-tenant Loki: the default X-Scope-OrgID, and what `tenant` may name.
        // LOKI_ORG_ID: "main",
        // LOKI_TENANTS: "main,edge",
        LOKI_MCP_PORT: "8511",
        LOKI_MCP_HOST: "127.0.0.1",
        LOKI_MCP_ALLOWED_HOSTS: "localhost:8511,127.0.0.1:8511,[::1]:8511",
        // Empty: stderr only, and PM2 keeps the log. Unset, loki-mcp writes a rotating
        // file under /opt/appdata/loki-mcp/logs.
        LOG_FILE: "",
      },
    },
  ],
};
