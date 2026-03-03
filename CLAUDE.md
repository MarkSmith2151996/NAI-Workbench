# NAI Workbench

This repo contains the setup, config, and tooling for a persistent dev environment accessed via **Wave Terminal** with a **Textual TUI dashboard**.

## Stack
- WSL2 Ubuntu 24.04 (NAT mode) + code-server + tmux + Docker
- **Wave Terminal** (Windows + Arch laptop) — split panes, web widgets, WSL/SSH integration
- **Textual TUI Dashboard** — real-time service health, Docker, system metrics
- Komodo dashboard (web UI for Docker, projects, scripts)
- Penpot (self-hosted Figma alternative — design/whiteboard)
- Claude CLI (runs natively in Wave terminal pane — MCP works out of the box)
- MCP servers: repomix, memory, filesystem
- Security: Trivy, Semgrep, Gitleaks, OWASP ZAP, k6
- **Remote access**: Tailscale (Windows system service) + `netsh interface portproxy` + OpenSSH (port 2223 internal / 2222 external)

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
Arch Laptop (100.79.63.10)
    │
    │  Tailscale VPN tunnel
    ▼
Windows PC (100.95.20.98)           ← Tailscale runs here as system service
    │
    │  netsh interface portproxy (binds 0.0.0.0:PORT)
    ▼
WSL2 Ubuntu (172.21.x.x NAT IP)    ← NAT mode, IP may change on reboot
    │
    ▼
Docker containers / systemd services
```

**Port proxy chain** (how external traffic reaches WSL):
- `0.0.0.0:9001` → `WSL_NAT_IP:9001` (Penpot)
- `0.0.0.0:9090` → `WSL_NAT_IP:9090` (Komodo)
- `0.0.0.0:9091` → `WSL_NAT_IP:9091` (code-server)
- `0.0.0.0:2222` → `127.0.0.1:2223` (sshd — uses localhost, immune to IP changes)

- **PC widgets**: `wsl://Ubuntu-24.04` connections, `localhost` URLs
- **Laptop widgets**: `ssh -t` commands in local terminal (NOT Wave's built-in SSH client), `http://TAILSCALE_IP:PORT` URLs

## Ports

| External | Internal | Service | Notes |
|----------|----------|---------|-------|
| `2222` | `127.0.0.1:2223` | sshd (OpenSSH) | Pubkey auth only. Immune to WSL IP changes |
| `9001` | `WSL_IP:9001` | Penpot | Docker maps `9001:8080` (nginx listens on 8080 inside container) |
| `9090` | `WSL_IP:9090` | Komodo | Docker maps `9090:9120` |
| `9091` | `WSL_IP:9091` | code-server | VS Code in browser |
| `7777` | `WSL_IP:7777` | Sandbox widget + /api/health | Alpha Builds preview iframe + health endpoint |

## Networking

- **WSL2 NAT mode** — WSL gets a private NAT IP (`172.21.x.x`) that may change on reboot
- **Port proxy** (`netsh interface portproxy`) forwards external ports from Windows (`0.0.0.0`) to WSL NAT IP
- **sshd is special**: external `2222` forwards to `127.0.0.1:2223` (not WSL NAT IP), so SSH survives IP changes
- **Mirrored networking** was tried and abandoned — causes port conflicts with `netsh portproxy` and Docker
- **Laptop widgets** use `ssh -t` commands in Wave's local terminal, NOT Wave's built-in SSH client (broken with Tailscale)
- See `docs/operations.md` for full networking details and troubleshooting

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
- Operations runbook: `docs/operations.md`
- Session context: `docs/session-context.md`

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
- `install-watchdog` — Install watchdog systemd service (sshd/Docker auto-recovery)
- `workbench-check` — Laptop connectivity checker (Tailscale, ports, SSH, health)

## Watchdog (`custodian/watchdog.py`)
Systemd daemon that auto-recovers sshd and Docker, cleans stale sandbox DB entries.
- Install: `bash bin/install-watchdog`
- Health: `cat /tmp/watchdog-health.json`
- Logs: `journalctl --user -u workbench-watchdog -f`

## Admin TUI Tabs (custodian/admin.py)
The Admin TUI has 8 tabs:
1. **Projects** — Import from GitHub, register local projects, view hierarchy maps
2. **Custodian** — Index projects (trigger Sonnet fossil generation)
3. **Fossils** — Browse fossil history, view architecture/symbols/issues
4. **Detective** — Run Sonnet/Opus analysis, view insights, refine prompts
5. **Status** — DB stats, project status, recent MCP queries
6. **Editor** — File browser + code editor + persistent Claude Code chat
7. **Agent Factory** — Create, configure, and run AI agents via Claude Agent SDK. Manage pipelines, approve reindex requests.
8. **Alpha Builds** — Docker container-based project sandboxes. Launch, stop, rebuild, shell into containers.

### MCP Tools
The Custodian MCP server (`custodian/mcp_server.py`) exposes these tools:
- `list_projects`, `get_project_fossil`, `lookup_symbol`, `get_symbol_context`
- `find_related_files`, `get_recent_changes`, `get_detective_insights`, `trigger_custodian`
- `request_reindex` — Create a pending reindex request (user must approve in Admin TUI)
- `sandbox_start/stop/restart/status/logs/test/install` — Sandbox management
- `penpot_list_projects/get_page/export_svg` — Penpot design integration

## How It Works
Wave Terminal is the primary interface. The TUI dashboard (`dashboard/dashboard.py`) runs in a Wave terminal pane showing service health, Docker containers, tmux sessions, projects, and system metrics with real-time auto-refresh. Claude CLI runs in a separate Wave pane with full MCP tool access. Penpot and Komodo load as Wave web widget panes. Wave config files in `config/wave/` define sidebar widget buttons for quick access.

## Auto-Start
`config/start-workbench.vbs` runs at Windows boot via Task Scheduler:
1. Docker + code-server
2. sshd on port 2223 (creates `/run/sshd` first)
3. Komodo (Docker compose)
4. Penpot (Docker compose — 5 containers)
5. Wave Terminal

**Persistent across reboots** (no VBS management needed):
- Tailscale — Windows system service, always on
- Port proxy rules (`netsh interface portproxy`) — survive reboots
- Windows Firewall rule ("NAI Workbench") — survives reboots
- Penpot containers — `restart: unless-stopped`

**Note**: `/run/sshd` disappears on every WSL restart; the VBS script recreates it. The VBS script needs to use port `2223` for sshd (not 2222).

## Penpot
- Self-hosted Figma alternative at port 9001
- 5 containers: frontend, backend, exporter, postgres, redis
- Compose file: `config/penpot/compose.yaml`
- Env config: `config/penpot/compose.env` (copy from `compose.env.template`, fill in secrets)
- Account: `admin@local.dev` / `admin123`
- Registration disabled after initial setup

## Laptop Bridge MCP Server

Remote MCP server that runs on the Arch laptop so the PC's Claude Code can operate on laptop files over Tailscale.

### First-time setup (run on the laptop)

```bash
cd ~/NAI-Workbench/laptop-bridge
bash install.sh
```

This installs deps (`mcp`, `uvicorn`, `starlette`), generates a token, creates a systemd user service, and starts it. Copy the printed token into the PC's `.claude/mcp.json` under `laptop-bridge.headers.Authorization`.

### Updating after code changes

```bash
cp ~/NAI-Workbench/laptop-bridge/server.py ~/laptop-bridge/server.py
systemctl --user restart laptop-bridge
```

### Service commands

```bash
systemctl --user status laptop-bridge
journalctl --user -u laptop-bridge -f
systemctl --user restart laptop-bridge
```

### Key details
- Binds to Tailscale IP only (`100.79.63.10:8222`)
- Bearer token auth on every request
- 8 tools: `laptop_read_file`, `laptop_write_file`, `laptop_edit_file`, `laptop_run_command`, `laptop_glob`, `laptop_grep`, `laptop_list_dir`, `laptop_system_info`
- Files: `laptop-bridge/server.py`, `laptop-bridge/install.sh`

## Known Gotchas
- `/run/sshd` disappears on WSL restart — watchdog auto-recovers this (install with `bash bin/install-watchdog`)
- WSL NAT IP can change on reboot — port proxy rules for 9001/9090/9091 need updating (2222 is fine, uses localhost)
- Penpot internal port is **8080** not 80 — Docker mapping must be `9001:8080`
- Claude Code needs browser auth once from the PC directly (not via SSH)
- Wave SSH integration is broken with Tailscale — laptop widgets use `ssh -t` in local terminal
- See `docs/operations.md` for full troubleshooting and recovery procedures
