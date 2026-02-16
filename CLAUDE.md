# NAI Workbench

This repo contains the setup, config, and tooling for a persistent browser-accessible dev environment.

## Stack
- WSL2 Ubuntu 24.04 + code-server + tmux + Docker
- MCP servers: repomix, memory, filesystem
- Security: Trivy, Semgrep, Gitleaks, OWASP ZAP, k6

## Key Paths
- Config files: config/
- Security hooks: hooks/
- Pipeline scripts: bin/
- Dev container templates: templates/

## How It Works
code-server serves VS Code in a browser. tmux keeps CLI sessions alive. MCP servers provide Claude with on-demand context. Pre-commit hooks run security scans before every commit.
