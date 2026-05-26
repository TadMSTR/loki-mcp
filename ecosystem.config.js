module.exports = {
  apps: [
    {
      name: "loki-mcp",
      script: "/home/ted/repos/personal/loki-mcp/.venv/bin/python",
      args: "-m loki_mcp.server",
      cwd: "/home/ted/repos/personal/loki-mcp",
      env: {
        LOKI_URL: "http://localhost:3100",
      },
      log_file: "/home/ted/logs/loki-mcp.log",
      error_file: "/home/ted/logs/loki-mcp-error.log",
      out_file: "/home/ted/logs/loki-mcp-out.log",
      restart_delay: 5000,
      autorestart: true,
    },
  ],
};
