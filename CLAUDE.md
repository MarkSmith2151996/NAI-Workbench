# NAI Workbench

This repo contains the setup, config, and tooling for a persistent browser-accessible dev environment and planning studio.

## Stack
- WSL2 Ubuntu 24.04 + code-server + tmux + Docker
- Komodo dashboard (web UI for Docker, projects, scripts)
- Excalidraw (whiteboard/diagramming)
- claude-code-webui (AI chat via Claude CLI)
- MCP servers: repomix, memory, filesystem, excalidraw
- Security: Trivy, Semgrep, Gitleaks, OWASP ZAP, k6

## Ports
- **9080** — Portal landing page (links to all tools)
- **9081** — Excalidraw whiteboard (diagrams, wireframes, flowcharts)
- **9082** — claude-code-webui (AI chat, brainstorming)
- **9090** — Komodo dashboard (Docker, system health, scripts)
- **9091** — code-server (VS Code in browser)

## Key Paths
- Portal page: `portal/`
- Config files: `config/`
- Komodo compose: `config/komodo/`
- Security hooks: `hooks/`
- Pipeline scripts: `bin/`
- Dev container templates: `templates/`

## Scripts (bin/)
- `workbench-status` — Full system status overview
- `list-sessions` — Show active tmux sessions
- `new-session <project> [claude]` — Create tmux session, optionally with Claude
- `kill-session <name>` — Kill a tmux session
- `open-project <name>` — Get code-server URL for a project
- `security-gate <dir> [url]` — Full 5-gate security pipeline
- `quick-scan <dir>` — Quick gitleaks + trivy scan

## How It Works
Portal (localhost:9080) is the home page — links to all tools. Excalidraw (localhost:9081) for visual brainstorming and diagrams. claude-code-webui (localhost:9082) for AI chat using Claude Max CLI. Komodo (localhost:9090) manages Docker and system health. code-server (localhost:9091) serves VS Code. MCP servers provide Claude with on-demand context including Excalidraw canvas access.

## Auto-Start
`D:\WSL\start-workbench.vbs` runs at Windows boot via Task Scheduler. Starts Docker, code-server, Komodo, and claude-webui. Docker containers with `--restart unless-stopped` auto-start (excalidraw-canvas, nai-portal).
