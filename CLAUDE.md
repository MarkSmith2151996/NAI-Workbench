# NAI Workbench

This repo contains the setup, config, and tooling for a persistent dev environment accessed via **Wave Terminal** with a **Textual TUI dashboard**.

## Stack
- WSL2 Ubuntu 24.04 + code-server + tmux + Docker
- **Wave Terminal** (Windows + Arch laptop) — split panes, web widgets, WSL/SSH integration
- **Textual TUI Dashboard** — real-time service health, Docker, system metrics
- Komodo dashboard (web UI for Docker, projects, scripts)
- Penpot (self-hosted Figma alternative — design/whiteboard)
- Claude CLI (runs natively in Wave terminal pane — MCP works out of the box)
- MCP servers: repomix, memory, filesystem
- Security: Trivy, Semgrep, Gitleaks, OWASP ZAP, k6
- **Remote access**: Tailscale (Windows app) + SSH on WSL port 2222

## Architecture

```
Wave Terminal (Windows PC — local, or Arch Laptop — via Tailscale SSH)
  ├─ Tab 1: "Workbench"
  │   ├─ Left pane: Textual TUI Dashboard
  │   └─ Right pane: Penpot (web widget → :9001)
  ├─ Tab 2: "Claude Studio"
  │   ├─ Left pane: Claude CLI (project picker)
  │   └─ Right pane: Penpot (web widget → :9001)
  └─ Tab 3: "Ops"
      ├─ Left pane: Terminal
      └─ Right pane: Komodo (web widget → :9090)
```

### Remote Access Architecture

```
Arch Laptop                          Windows PC
┌─────────────┐    Tailscale VPN    ┌──────────────────┐
│ Wave Terminal│◄──────────────────► │ Tailscale (Win)  │
│ (thin client)│   100.x.x.x        │                  │
│              │                     │ WSL2 services:   │
│ widgets use  │   ssh://dev@...:2222│  ├─ sshd   :2222 │
│ ssh:// conn  │                     │  ├─ Penpot :9001 │
│ web widgets  │   http://100.x:PORT │  ├─ Komodo :9090 │
└─────────────┘                     │  └─ code-sv:9091 │
                                    └──────────────────┘
```

- **Tailscale runs on Windows** (not WSL) — proper system service, always online
- WSL2 auto-forwards ports to Windows, so WSL services are reachable via Tailscale IP
- **PC widgets**: `wsl://Ubuntu-24.04` connections, `localhost` URLs
- **Laptop widgets**: `ssh://dev@TAILSCALE_IP:2222` connections, `http://TAILSCALE_IP:PORT` URLs
- Laptop config uses `TAILSCALE_IP` placeholder, replaced with `sed` during setup

## Ports
- **2222** — SSH server (OpenSSH in WSL, pubkey auth only)
- **9001** — Penpot design tool (self-hosted Figma alternative)
- **9090** — Komodo dashboard (Docker, system health, scripts)
- **9091** — code-server (VS Code in browser)

## Key Paths
- TUI dashboard: `dashboard/`
- Wave config (PC, source of truth): `config/wave/widgets.json`
- Wave config (laptop template): `config/wave/widgets-laptop.json`
- Wave connections (PC): `config/wave/connections.json`
- Wave connections (laptop template): `config/wave/connections-laptop.json`
- Config files: `config/`
- Komodo compose: `config/komodo/`
- Penpot compose: `config/penpot/`
- Security hooks: `hooks/`
- Pipeline scripts: `bin/`
- Dev container templates: `templates/`
- Laptop setup guide: `docs/laptop-setup.md`

## Scripts (bin/)
- `workbench-status` — Full system status overview
- `studio-status` — Service health check
- `launch-dashboard` — Start the Textual TUI dashboard
- `new-session <project> [claude]` — Create tmux session, optionally with Claude
- `kill-session <name>` — Kill a tmux session
- `open-project <name>` — Get code-server URL for a project
- `security-gate <dir> [url]` — Full 5-gate security pipeline
- `quick-scan <dir>` — Quick gitleaks + trivy scan
- `test-project` — Interactive test pipeline: pick a project → auto-detect stack → lint/type-check/test/coverage/security → AI debug on failure
- `claude-session` — Project picker that launches Claude CLI in the selected project dir
- `import-project` — Clone a GitHub repo into ~/projects/ with optional hooks + CLAUDE.md

## How It Works
Wave Terminal is the primary interface. The TUI dashboard (`dashboard/dashboard.py`) runs in a Wave terminal pane showing service health, Docker containers, tmux sessions, projects, and system metrics with real-time auto-refresh. Claude CLI runs in a separate Wave pane with full MCP tool access. Penpot and Komodo load as Wave web widget panes. Wave config files in `config/wave/` define sidebar widget buttons for quick access.

## Auto-Start
`config/start-workbench.vbs` runs at Windows boot via Task Scheduler:
1. Docker + code-server
2. sshd on port 2222
3. Komodo (Docker compose)
4. Penpot (Docker compose — 5 containers)
5. Wave Terminal

Tailscale runs as a Windows system service (installed separately) — always on, no VBS management needed. Penpot containers have `restart: unless-stopped` so they auto-start with Docker.

## Penpot
- Self-hosted Figma alternative at port 9001
- 5 containers: frontend, backend, exporter, postgres, redis
- Compose file: `config/penpot/compose.yaml`
- Env config: `config/penpot/compose.env` (copy from `compose.env.template`, fill in secrets)
- Account: `admin@local.dev` / `admin123`
- Registration disabled after initial setup
