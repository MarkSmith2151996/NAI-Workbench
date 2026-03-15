# NAI Workbench

A persistent, multi-device development environment built on **WSL2 + Wave Terminal + Claude Code + MCP**. One Windows PC runs everything; remote devices (laptops, Pixelbooks) connect as thin clients over Tailscale VPN.

## Why This Exists

Every project you work on gets:
- An **AI-generated architectural fossil** (Claude Sonnet analyzes the codebase and writes a structured summary)
- **23 MCP tools** that any Claude Code session can call — query fossils, manage Docker sandboxes, create/run AI agents, read Penpot designs, operate on remote devices
- A **Textual TUI dashboard** with 8 tabs for managing everything
- **Docker sandboxes** with live preview in Wave Terminal
- **Security scanning** on every commit (Gitleaks, Semgrep, Trivy)

---

## Architecture Overview

```
Windows PC (host)
  └─ WSL2 Ubuntu 24.04 (NAT mode)
      ├─ Custodian system (SQLite DB + MCP server + indexing pipeline)
      ├─ Docker containers (project sandboxes, Penpot, Komodo)
      ├─ sshd on port 2223 (external 2222 via netsh portproxy)
      └─ Watchdog daemon (auto-recovers sshd + Docker)

Wave Terminal (runs on each device)
  ├─ Sidebar widgets → SSH terminals + web widgets
  └─ Claude Code pane → MCP tools via stdio

Remote Devices (Arch laptop, Pixelbook, etc.)
  └─ Tailscale VPN → SSH to PC → all services
```

### How Devices Connect

```
Remote Device
    │  Tailscale VPN tunnel
    ▼
Windows PC (100.95.20.98)
    │  netsh portproxy (binds 0.0.0.0:PORT)
    ▼
WSL2 (172.21.x.x NAT IP)
    │
    ▼
Services (Docker, sshd, sandbox router, etc.)
```

---

## Components

### Wave Terminal Sidebar Widgets

The primary UI. Each device runs Wave Terminal locally with sidebar buttons that open panes:

| Widget | Type | Description |
|--------|------|-------------|
| **Admin** | Terminal (TUI) | 8-tab Textual TUI — project management, fossils, detective, editor, agents, sandboxes |
| **Editor** | Terminal | Project picker → Claude Code with MCP tools |
| **Draw** | Web | Excalidraw (browser-based whiteboard/drawing tool) |
| **Sandbox** | Web (:7777) | Live preview of running Docker sandboxes + status ticker |
| **Notes** | Terminal (TUI) | Persistent sticky notes |
| **Terminal** | Terminal | Raw WSL shell |
| **PowerShell** | Terminal | Windows PowerShell (PC only) |

Widget configs:
- **PC** (source of truth): `~/.config/waveterm/widgets.json` on Windows
- **Templates**: `config/wave/widgets.json` (PC), `config/wave/widgets-laptop.json` (remote devices with `TAILSCALE_IP` placeholder)

### Custodian (AI Indexing System)

The core intelligence layer. Custodian indexes registered projects with Claude Sonnet to produce **fossils** — structured JSON snapshots of a codebase's architecture, symbols, dependencies, and issues.

**Indexing Pipeline:**
```
index_project.sh
  → repomix (bundles codebase into single file)
  → git log (recent commits)
  → Claude Sonnet API (generates fossil JSON)
  → parse_symbols.py (tree-sitter extracts functions/classes/types)
  → store_fossil.py → custodian.db
```

**What a fossil contains:**
- `summary` — one-paragraph project description
- `architecture` — data flow, entry points, component relationships
- `file_tree` — every file with description and line count
- `dependencies` — packages with versions and purposes
- `known_issues` — bugs, TODOs, tech debt with file/line references
- `symbols` — every function, class, type with signatures and relationships

### MCP Server (`custodian/mcp_server.py`)

Exposes **23 tools** to Claude Code over stdio. Any Claude session with this MCP server registered can:

**Knowledge tools (8):**
| Tool | Description |
|------|-------------|
| `list_projects` | All registered projects with status |
| `get_project_fossil` | Full architecture fossil for a project |
| `lookup_symbol` | Live tree-sitter search — current file paths and line numbers |
| `get_symbol_context` | Sonnet's descriptions and relationship analysis for a symbol |
| `find_related_files` | Files that would need changes for a given symbol/concept |
| `get_recent_changes` | Summarized recent commits |
| `get_detective_insights` | Coupling patterns, warnings, architectural insights |
| `trigger_custodian` | Re-index a project (async) |

**Sandbox tools (8):**
| Tool | Description |
|------|-------------|
| `sandbox_start` | Start a Docker sandbox (auto-detects npm/python) |
| `sandbox_stop` | Stop running sandbox |
| `sandbox_restart` | Restart sandbox |
| `sandbox_status` | PID, port, error count |
| `sandbox_logs` | Recent output, filter by error/warning |
| `sandbox_test` | Run test suite (auto-detects) |
| `sandbox_install` | Install extra packages in container |
| `sandbox_exec` | Run arbitrary command in container |

**Agent Factory tools (6):**
| Tool | Description |
|------|-------------|
| `agent_list` | List all agents with status and run counts |
| `agent_create` | Create a new persistent AI agent (name, system prompt, model, project binding) |
| `agent_update` | Update agent config |
| `agent_delete` | Soft-delete an agent |
| `agent_run` | Run an agent via Claude CLI subprocess — returns output, tokens, cost |
| `agent_runs` | View run history |

**Other tools:**
| Tool | Description |
|------|-------------|
| `request_reindex` | Request fossil reindex (user approves in Admin TUI) |
| `penpot_list_projects` | List Penpot design projects |
| `penpot_get_page` | Get component structure of a Penpot page |
| `penpot_export_svg` | Export Penpot page as SVG |
| `laptop_*` (7 tools) | Remote file/command access on paired devices over Tailscale |

### Admin TUI (`custodian/admin.py`)

8-tab Textual TUI application:

1. **Projects** — Import from GitHub, register local projects, view hierarchy maps
2. **Custodian** — Trigger indexing, monitor indexing runs
3. **Fossils** — Browse fossil history, view architecture/symbols/issues
4. **Detective** — Run Sonnet/Opus analysis, view coupling insights, refine prompts
5. **Status** — DB stats, project status, recent MCP queries
6. **Editor** — File browser + code editor + persistent Claude Code chat sessions
7. **Agent Factory** — Create/configure/run AI agents, manage pipelines, approve reindex requests
8. **Alpha Builds** — Docker sandbox management — launch, stop, rebuild, shell into containers

### Agent Factory

Persistent AI agents stored in the shared SQLite database. Created from any Claude session, visible everywhere.

**Use cases:**
- Repeatable tasks (code review, test generation, data analysis)
- Specialized roles with custom system prompts
- Multi-step workflows where agents handle different stages
- Tracking execution history, token usage, and cost

**Example — creating an agent:**
```
agent_create(
  name="code-reviewer",
  system_prompt="You review code for bugs, security issues, and style. Be concise.",
  model="sonnet",
  project="my-project"
)
```

**Example — running it:**
```
agent_run(agent="code-reviewer", prompt="Review the auth middleware for security issues")
```

Agents run as `claude -p` subprocesses. Runs are tracked with status, output, tokens, errors, and timestamps.

### Docker Sandboxes (Alpha Builds)

Each registered project can have a Docker container (`alpha-{project}`) that mounts the project at `/workspace`. The sandbox system:

- Auto-detects stack (Python/Node) and runs appropriate dev server
- Streams output to `/tmp/sandbox.log` inside the container
- Serves a live preview dashboard on port 7777 (the Sandbox widget)
- Self-heals: detects stale containers, corrects DB state automatically
- Smart port defaults: web server commands auto-get port 8080

### Sandbox Router (`custodian/sandbox_router.py`)

HTTP server on port 7777 that:
- Serves the Sandbox preview widget (HTML + JS dashboard)
- Provides `/api/status` (current sandbox state) and `/api/workbench` (system status)
- Provides `/api/health` endpoint for remote connectivity checks
- Rewrites `localhost` URLs to the client's Host header for Tailscale access
- Verifies Docker containers are actually alive before reporting status

### Watchdog (`custodian/watchdog.py`)

Systemd user daemon that runs a 10-second health check cycle:
- **sshd monitoring** — auto-creates `/run/sshd` and restarts sshd if down
- **Docker monitoring** — restarts Docker service if unresponsive
- **Stale sandbox cleanup** — marks dead containers as stopped in the DB
- Writes health to `/tmp/watchdog-health.json` (read by sandbox router)

Install: `bash bin/install-watchdog`

### Security Pipeline

Pre-commit hooks and on-demand scanning:
- **Gitleaks** — secret detection
- **Semgrep** — static analysis (OWASP rules)
- **Trivy** — vulnerability scanning

Full pipeline: `bin/security-gate <dir> [url]`
Quick scan: `bin/quick-scan <dir>`

---

## Database Schema

Central SQLite database at `custodian/custodian.db`. Key tables:

| Table | Purpose |
|-------|---------|
| `projects` | Registered projects (name, path, stack, status) |
| `fossils` | AI-generated architectural snapshots |
| `symbols` | Tree-sitter extracted functions/classes/types per fossil |
| `detective_insights` | Coupling patterns, growth analysis, warnings |
| `agents` | Persistent AI agent definitions |
| `agent_runs` | Agent execution history with tokens/cost |
| `alpha_builds` | Docker sandbox container state |
| `sandbox_state` | Active sandbox processes |
| `editor_sessions` | Persistent Claude Code sessions per project |
| `sticky_notes` | Notes widget data |
| `devices` | Paired remote devices |
| `reindex_requests` | Pending fossil reindex requests |
| `indexing_runs` | Indexing pipeline execution log |
| `query_log` | MCP tool usage log |

Schema definition: `custodian/schema.sql`

---

## Network & Ports

| External Port | Internal | Service | Notes |
|---------------|----------|---------|-------|
| `2222` | `127.0.0.1:2223` | sshd (OpenSSH) | Pubkey auth only. Immune to WSL IP changes |
| `7777` | `WSL_IP:7777` | Sandbox widget + `/api/health` | Preview iframe + health endpoint |
| `9001` | `WSL_IP:9001` | Penpot | Docker maps `9001:8080` |
| `9090` | `WSL_IP:9090` | Komodo | Docker dashboard |
| `9091` | `WSL_IP:9091` | code-server | VS Code in browser |

Port proxy is configured via `netsh interface portproxy` on Windows. Rules survive reboots. WSL NAT IP may change on reboot — ports 9001/9090/9091/7777 need updating (2222 uses localhost, immune).

---

## File Structure

```
NAI-Workbench/
├── bin/                          # Executable scripts
│   ├── admin-session             # Launches Admin TUI in venv
│   ├── editor-session            # Launches Editor TUI in venv
│   ├── claude-session            # Project picker → Claude CLI
│   ├── sandbox-session           # Attaches to running sandbox
│   ├── notes-session             # Launches sticky notes TUI
│   ├── status-ticker             # Launches status ticker TUI
│   ├── custodian                 # CLI for custodian operations
│   ├── install-watchdog          # Install watchdog systemd service
│   ├── workbench-check           # Laptop connectivity diagnostics
│   ├── setup-device              # Remote device setup script
│   ├── import-project            # Clone GitHub repo + hooks
│   ├── new-session / kill-session # tmux session management
│   ├── security-gate / quick-scan # Security pipeline scripts
│   ├── test-project              # Interactive test pipeline
│   └── workbench-status / studio-status / launch-dashboard
│
├── custodian/                    # Core system
│   ├── admin.py                  # 8-tab Textual TUI (157K lines)
│   ├── mcp_server.py             # MCP server — 23 tools (117K)
│   ├── editor.py                 # Editor TUI with Claude chat
│   ├── sandbox_router.py         # HTTP server on :7777
│   ├── watchdog.py               # Systemd health daemon
│   ├── detective.py              # AI analysis engine
│   ├── index_project.sh          # Indexing pipeline entry point
│   ├── parse_symbols.py          # Tree-sitter symbol extraction
│   ├── store_fossil.py           # Fossil → SQLite writer
│   ├── schema.sql                # Full database schema
│   ├── init_db.py                # DB initialization
│   ├── sandbox.py                # Legacy sandbox (superseded by Alpha Builds)
│   ├── status_ticker.py          # Status ticker TUI
│   ├── sticky_notes.py           # Sticky notes TUI
│   └── custodian.db              # SQLite database (runtime)
│
├── config/
│   ├── wave/
│   │   ├── widgets.json          # PC sidebar widget definitions
│   │   ├── widgets-laptop.json   # Remote device template (TAILSCALE_IP placeholder)
│   │   ├── connections.json      # PC Wave connections
│   │   ├── connections-laptop.json # Remote device connections template
│   │   └── settings.json         # Wave terminal settings
│   ├── penpot/                   # Penpot Docker compose + env
│   ├── komodo/                   # Komodo Docker compose
│   ├── start-workbench.vbs       # Windows auto-start script
│   ├── tmux.conf                 # tmux configuration
│   ├── code-server.yaml          # code-server config
│   └── mcp.json                  # Claude MCP server registration
│
├── dashboard/
│   └── dashboard.py              # Legacy standalone dashboard TUI
│
├── docs/
│   ├── remote-device-setup.md    # How to connect new devices
│   ├── EDITOR_PLAN.md            # Editor tab design doc
│   └── operations.md             # Networking troubleshooting
│
├── hooks/
│   ├── pre-commit                # Security scanning hook
│   └── install-hooks.sh          # Hook installer
│
├── laptop-bridge/
│   ├── server.py                 # MCP server running on Arch laptop (Tailscale)
│   └── install.sh                # Laptop bridge installer
│
├── templates/                    # Dev container templates
│   ├── python/.devcontainer/     # Python Dockerfile + devcontainer.json
│   ├── node/.devcontainer/       # Node Dockerfile + devcontainer.json
│   └── project-claude.md         # Template CLAUDE.md for new projects
│
├── install.sh                    # Full system installer (WSL2)
├── CLAUDE.md                     # Instructions for Claude Code sessions
└── README.md                     # This file
```

---

## Setup

### Fresh Install (PC with WSL2)

```bash
git clone https://github.com/MarkSmith2151996/NAI-Workbench.git ~/NAI-Workbench
cd ~/NAI-Workbench
bash install.sh
```

This installs: system packages, Node.js 22, Docker, code-server, Claude Code, MCP servers, security tools, and OpenSSH.

### Add a Remote Device

See [docs/remote-device-setup.md](docs/remote-device-setup.md) for step-by-step instructions to connect any device (Pixelbook, laptop, second PC) via Tailscale + SSH + Wave Terminal.

### Install Watchdog

```bash
bash bin/install-watchdog
```

Auto-recovers sshd and Docker. Check health: `cat /tmp/watchdog-health.json`

### Register MCP Server

On the PC (or via SSH from any device):
```bash
claude mcp add-json --scope user custodian \
  '{"command":"/home/dev/.custodian-venv/bin/python3","args":["/home/dev/projects/nai-workbench/custodian/mcp_server.py"],"env":{"PYTHONPATH":"/home/dev/projects/nai-workbench/custodian"}}'
```

---

## Auto-Start

`config/start-workbench.vbs` runs at Windows boot via Task Scheduler:
1. Docker + code-server
2. sshd on port 2223 (creates `/run/sshd` first)
3. Komodo (Docker compose)
4. Penpot (Docker compose — 5 containers)
5. Wave Terminal

**Persist across reboots automatically:**
- Tailscale — Windows system service
- Port proxy rules (`netsh interface portproxy`)
- Windows Firewall rule ("NAI Workbench")
- Penpot containers (`restart: unless-stopped`)

---

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `textual` | TUI framework for admin, editor, notes, dashboard |
| `anthropic` | Claude API client for detective analysis and indexing |
| `mcp` | MCP protocol library for the tool server |
| `tree-sitter` + grammars | Symbol extraction from source code |
| `repomix` (npx) | Codebase bundler for fossil generation |
| `docker` | Container runtime for sandboxes |
| `tailscale` | VPN for multi-device access |
| `wave-terminal` | Primary UI — split panes, web widgets, SSH |

Python venv: `~/.custodian-venv/` (used by all session scripts)

---

## For Claude Code Sessions

Every Claude Code session working on this project should:

1. Call `get_project_fossil('nai-workbench')` first to load architecture context
2. Use `lookup_symbol(project, symbol)` for live line numbers (tree-sitter)
3. Read files and make changes with Edit/Write tools
4. Use `sandbox_start`/`sandbox_test` to run and test
5. Use `sandbox_logs` to check for errors

See `CLAUDE.md` for full instructions, including Agent Factory usage and known gotchas.

---

## Known Gotchas

- `/run/sshd` disappears on WSL restart — watchdog auto-recovers this
- WSL NAT IP changes on reboot — port proxy rules for 9001/9090/9091/7777 need updating (2222 is immune)
- Session scripts must use venv python (`~/.custodian-venv/bin/python3`), not bare `python3`
- Session scripts must have LF line endings (not CRLF) — use `sed -i 's/\r$//'` if needed
- `sqlite3.Row` does NOT support `.get()` — use `row["key"]` instead
- After editing `mcp_server.py`, kill the process and run `/mcp` to reload
- Claude Code needs browser auth once from the PC directly (not via SSH)

## License

MIT
