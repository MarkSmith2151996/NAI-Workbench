# NAI Workbench Architecture Map

This document is the current architecture map for `nai-workbench`.

It is meant to stay useful while the system changes quickly. It focuses on:
- the stable system shape
- where responsibility lives
- how data moves
- which areas are actively evolving
- what assumptions are currently fragile

## Core Model

NAI Workbench is a host-level development control plane.

It does five jobs at once:
- provides terminal and web entrypoints through Wave Terminal
- stores shared state for all registered projects in a central SQLite database
- exposes that state and related automation through an MCP server
- manages per-project sandboxes, agents, memory, and indexing
- supports remote devices over Tailscale with SSH and a laptop bridge

The center of gravity is `custodian/`, not `dashboard/`.

## Layered View

```text
Wave Terminal / Remote Devices / OpenCode / Claude CLI
    |
    +-- Terminal UIs
    |   +-- custodian/admin.py
    |   +-- custodian/editor.py
    |   +-- custodian/sticky_notes.py
    |   +-- dashboard/dashboard.py
    |
    +-- Web UIs
    |   +-- custodian/sandbox_router.py  (:7777)
    |   +-- Penpot                       (:9001)
    |   +-- Komodo                       (:9090)
    |   +-- code-server                  (:9091)
    |
    +-- AI / Automation Surface
    |   +-- custodian/mcp_server.py      (stdio MCP server)
    |   +-- laptop-bridge/server.py      (remote HTTP/SSE MCP server)
    |
    +-- Shared State
    |   +-- custodian/custodian.db
    |   +-- custodian/schema.sql
    |
    +-- Ops / Boot / Recovery
        +-- config/start-workbench.vbs
        +-- custodian/watchdog.py
        +-- bin/install-watchdog
        +-- bin/install-sandbox-router
        +-- ticker overlay + notifier
```

## System Boundaries

### 1. Experience Layer

This is how a human touches the system.

#### Wave widgets
Primary widget definitions live in:
- `config/wave/widgets.json`
- `config/wave/widgets-laptop.json`

Current widget set on the PC includes:
- Admin
- Editor
- Sandbox
- Notes
- Terminal
- PowerShell
- Draw (`tldraw`)
- Claude Browser
- OpenCode

Important point: widget config is now broader than the older docs describe.

#### Admin TUI
File: `custodian/admin.py`

This is the main operator console. It currently owns 10 tabs:
- Projects
- Custodian
- Fossils
- Detective
- Status
- Editor
- Agent Factory
- Alpha Builds
- Devices
- Ticker

What it does:
- project registration/import and hierarchy browsing
- indexing triggers and status viewing
- fossil browsing
- detective analysis entrypoints
- editor/file browsing and Claude session orchestration
- agent and pipeline CRUD/run history
- sandbox launch/stop/test/install/logs
- device pairing and revocation
- ticker segment ordering and overlay settings

The Admin TUI is a large orchestration surface, not just a viewer.

#### Editor TUI
File: `custodian/editor.py`

This is the focused project picker for launching Claude sessions with project context.

What it does:
- lists active registered projects from the DB
- shows fossil/session metadata per project
- creates or resumes persistent `editor_sessions`
- builds a project-specific system prompt
- launches `claude` with a session id and appended prompt

This is a thinner surface than `admin.py`, optimized for entering work.

#### Sticky Notes
File: `custodian/sticky_notes.py`

Minimal local notes UI backed by the shared database.

#### Standalone Dashboard
File: `dashboard/dashboard.py`

This is a Textual ops dashboard for services, Docker, tmux, projects, and system metrics.

Important architectural note:
- it is still useful
- but it is no longer the primary system brain
- it is more of a secondary operations view than the core control surface

### 2. Control Layer

This is where commands, queries, and orchestration logic live.

#### MCP server
File: `custodian/mcp_server.py`

This is the main automation backend.

It currently exposes several families of tools:
- knowledge tools
- sandbox tools
- agent tools
- memory tools
- Penpot tools
- laptop tools
- reindex request tooling

It also does more than simple query serving:
- opens SQLite directly
- runs lightweight schema migrations at startup
- starts the sandbox router in fallback mode if `:7777` is not already bound
- shells out to Docker, Claude CLI, and project tooling
- proxies laptop actions to the remote laptop bridge

Architecturally, this file is both:
- an MCP transport endpoint
- a service layer
- a partial runtime bootstrapper

That makes it powerful, but also means it is a high-change, high-coupling file.

#### Sandbox router
File: `custodian/sandbox_router.py`

This is the HTTP/web layer for sandbox and workbench status.

It serves:
- the sandbox UI on `/`
- ticker-only UI on `/ticker`
- `/api/status`
- `/api/logs`
- `/api/workbench`
- `/api/ticker-config`
- `/api/ticker-settings`
- `/api/health`
- `/api/devices`
- `/setup`
- `/api/pair/generate`
- `/api/pair`

It acts as a small integration hub between:
- alpha build state in SQLite
- Docker container liveness
- watchdog health file
- Claude session activity from `~/.claude/debug`
- pairing/device registration flows
- ticker overlay consumers

This file is more than a preview server now. It is becoming a local HTTP control API.

#### Laptop bridge
Files:
- `laptop-bridge/server.py`
- `laptop-bridge/stdio-proxy.py`

This is the remote execution boundary for the paired laptop.

Responsibilities:
- expose file and command tools on the remote laptop
- enforce bearer-token auth
- apply a denylist for sensitive paths
- rate-limit requests
- provide either HTTP/SSE access or stdio proxy access

The PC-side `mcp_server.py` uses this as a remote capability extension.

### 3. State Layer

The shared source of truth is:
- `custodian/custodian.db`

Schema definition lives in:
- `custodian/schema.sql`

Core state domains:
- `projects`: registered projects across the whole workbench
- `fossils`: AI-generated architecture snapshots
- `symbols`: tree-sitter extracted symbols per fossil
- `detective_insights`: analysis output
- `query_log`: tool usage history
- `editor_sessions`: persistent Claude session tracking
- `sandbox_state`: older sandbox process model
- `alpha_builds`: current Docker-backed sandbox model
- `agents`, `pipelines`, `agent_runs`: Agent Factory
- `devices`, `pairing_codes`: remote device pairing
- `memories` + FTS: persistent shared memory
- `ticker_config`, `ticker_settings`: ticker behavior and presentation
- `sticky_notes`: notes widget content
- `reindex_requests`, `indexing_runs`: indexing workflow state

Important architectural point:

`custodian.db` is not just storage. It is the coordination fabric between:
- UI surfaces
- MCP tools
- sandboxes
- agents
- remote access features
- session management

### 4. Ops Layer

#### Boot orchestration
File: `config/start-workbench.vbs`

This is the Windows-side startup entrypoint.

It currently:
- discovers the current WSL NAT IP
- recreates `netsh interface portproxy` rules
- starts Docker and code-server inside WSL
- starts sshd inside WSL
- starts Komodo
- starts Penpot
- launches Wave Terminal
- launches the ticker overlay with `pythonw.exe`

This makes it the bridge between Windows host concerns and WSL runtime concerns.

#### Watchdog
File: `custodian/watchdog.py`

This is the self-healing daemon.

It runs on a timed loop and checks:
- sshd health and `/run/sshd`
- Docker health
- WSL IP observation
- stale alpha build records

It writes health to `/tmp/watchdog-health.json`, which is then consumed by the router.

This is important: the router does not own health checks directly. It reads the watchdog output.

#### Service installers
Files:
- `bin/install-watchdog`
- `bin/install-sandbox-router`

These turn Python scripts into user-level systemd services.

This is a key architectural pattern in the repo:
- Python module provides behavior
- `bin/` installer provides persistence and lifecycle management

### 5. External Services Layer

These are not the repo's core logic, but the system depends on them heavily:
- Penpot at `config/penpot/`
- Komodo at `config/komodo/`
- code-server via systemd/config
- Docker for alpha builds and service stacks
- Claude CLI for editor and agent runs
- OpenCode via `bin/opencode-session`
- Tailscale for remote reachability
- Wave Terminal for composition of the whole UX

## Major Runtime Flows

### A. Project Work Flow

```text
User -> Wave widget / Editor / Admin
    -> project selected from DB
    -> Claude or OpenCode session launched in project directory
    -> MCP tools talk to custodian/mcp_server.py
    -> mcp_server.py reads/writes custodian.db and shells out as needed
```

Files involved:
- `bin/editor-session`
- `custodian/editor.py`
- `custodian/mcp_server.py`
- `custodian/schema.sql`

### B. Fossil / Indexing Flow

```text
Admin or MCP trigger
    -> indexing pipeline shell script(s)
    -> repo packaged + recent git history gathered
    -> Sonnet generates fossil
    -> symbols parsed
    -> fossil + symbols stored in SQLite
    -> UI/MCP surfaces read results later
```

Main files:
- `custodian/admin.py`
- `custodian/mcp_server.py`
- `custodian/index_project.sh`
- `custodian/parse_symbols.py`
- `custodian/store_fossil.py`

### C. Sandbox Flow

```text
User or MCP calls sandbox_start
    -> mcp_server.py resolves project + command
    -> Docker container launched / tracked in alpha_builds
    -> sandbox_router.py exposes status + logs + preview URL on :7777
    -> Wave Sandbox widget renders router UI
    -> watchdog later corrects stale DB state if container dies
```

Main files:
- `custodian/mcp_server.py`
- `custodian/sandbox_router.py`
- `custodian/watchdog.py`
- `bin/install-sandbox-router`

### D. Device Pairing Flow

```text
Admin generates pairing code
    -> code stored in pairing_codes
    -> remote device fetches /setup or runs setup-device
    -> device POSTs code + SSH pubkey + metadata to /api/pair
    -> router validates code, stores device, appends authorized_keys
    -> device can SSH to the PC-hosted workbench
```

Main files:
- `custodian/admin.py`
- `custodian/sandbox_router.py`
- `bin/setup-device`

### E. Ticker / Notification Flow

```text
workbench state changes
    -> router exposes /api/workbench and ticker settings
    -> ticker_overlay.py polls router for visual overlay content
    -> ticker_notifier.py polls router for toast-worthy state changes
    -> status_ticker.py separately updates terminal title bars
```

This is currently a three-surface status system:
- browser/widget ticker inside the router page
- Windows overlay ticker
- terminal title ticker

Files involved:
- `custodian/sandbox_router.py`
- `custodian/ticker_overlay.py`
- `custodian/ticker_notifier.py`
- `custodian/status_ticker.py`
- `bin/launch-ticker`

## Component Map By Directory

### `custodian/`
Primary system domain.

Important modules:
- `admin.py`: operator console
- `editor.py`: Claude project launcher
- `mcp_server.py`: MCP backend and tool runtime
- `sandbox_router.py`: HTTP API and sandbox UI
- `watchdog.py`: recovery daemon
- `detective.py`: architecture analysis logic
- `parse_symbols.py`: tree-sitter symbol extraction
- `store_fossil.py`: fossil persistence
- `status_ticker.py`: terminal title status
- `ticker_overlay.py`: desktop overlay
- `ticker_notifier.py`: desktop notifications
- `sticky_notes.py`: notes widget
- `schema.sql`: system model
- `custodian.db`: shared state

### `bin/`
Operational entrypoints and installers.

Patterns in this folder:
- session launchers: `admin-session`, `editor-session`, `notes-session`, `opencode-session`
- lifecycle installers: `install-watchdog`, `install-sandbox-router`
- health/status scripts: `workbench-status`, `studio-status`, `workbench-check`
- device setup: `setup-device`, `ssh-widget`
- project utilities: `import-project`, `new-session`, `test-project`

### `config/`
Host/runtime integration.

Main areas:
- `wave/`: UX composition
- `penpot/`: design service stack
- `komodo/`: ops service stack
- `start-workbench.vbs`: Windows bootstrap
- `mcp.json`: older MCP config shape

### `dashboard/`
Standalone Textual dashboard.

Still part of the repo, but not the architectural center anymore.

### `laptop-bridge/`
Remote capability extension for the paired laptop.

## Stable Foundations Vs Fast-Moving Areas

### Relatively stable foundations
- SQLite-centered architecture
- Wave + WSL + Tailscale deployment model
- `custodian/` as the system core
- `mcp_server.py` as the primary automation entrypoint
- `sandbox_router.py` on port `7777`
- project registry and fossil model

### Fast-moving areas
- ticker overlay / notifier / status surfaces
- device pairing UX and remote setup flow
- Wave widget composition
- Agent Factory depth and pipeline behavior
- alpha build lifecycle and sandbox UX
- editor/admin overlap and UX boundaries
- Windows vs WSL path normalization strategy

## Current Architectural Realities

### 1. The system is intentionally centralized

A lot of behavior is routed through a few large files:
- `custodian/admin.py`
- `custodian/mcp_server.py`
- `custodian/sandbox_router.py`

This is good for speed of iteration, but it creates coupling pressure.

### 2. The DB is shared across almost everything

That is a strength, but it also means schema changes ripple widely.

Any change to these domains can affect multiple surfaces at once:
- project records
- alpha builds
- editor sessions
- devices
- memories
- ticker settings

### 3. Path translation is a real design constraint

The system currently spans:
- WSL-native paths
- Windows-native paths stored in DB rows
- remote laptop Linux paths

This is why several modules implement `_to_native_path()` or path-conversion logic.

Do not assume a single-path world when changing project/file features.

### 4. Docs lag the code

Known drift today:
- docs still describe fewer Admin tabs than the code actually has
- widget docs lag the live widget config
- some MCP tool counts in docs are stale
- path examples still mix old clone locations and current workspace locations

This architecture map should be treated as closer to reality than the older overview docs.

## Operator Use

This section is here to make the map useful during active implementation work.

### Trust Order

When sources disagree, trust them in this order:
- current code in `custodian/`, `bin/`, and `config/`
- current widget/config/runtime files
- current git status and recent commits
- this architecture map
- older overview docs like `README.md`, `CLAUDE.md`, and older session notes

Reason: the repo is evolving faster than the docs.

### Read Order By Task

If the task is mostly UI/operator workflow:
- `custodian/admin.py`
- `config/wave/widgets.json`
- `bin/admin-session`

If the task is mostly AI tools / Claude / OpenCode integration:
- `custodian/mcp_server.py`
- `custodian/editor.py`
- `.claude/mcp-wsl.json`
- `bin/opencode-session`

If the task is mostly sandbox or preview behavior:
- `custodian/mcp_server.py`
- `custodian/sandbox_router.py`
- `custodian/watchdog.py`
- `bin/install-sandbox-router`

If the task is mostly boot, services, or connectivity:
- `config/start-workbench.vbs`
- `custodian/watchdog.py`
- `docs/operations.md`
- `bin/workbench-check`

If the task is mostly remote-device pairing or laptop support:
- `custodian/sandbox_router.py`
- `bin/setup-device`
- `laptop-bridge/server.py`
- `config/wave/widgets-laptop.json`

If the task is mostly shared state or feature persistence:
- `custodian/schema.sql`
- `custodian/mcp_server.py`
- `custodian/admin.py`
- `custodian/sandbox_router.py`

### Working Invariants

These are the assumptions that currently shape most changes:
- `custodian.db` is the main coordination layer
- `mcp_server.py` is the main automation/control surface
- `sandbox_router.py` is the main local HTTP surface
- Wave is the main user-facing shell around the system
- project paths may be WSL paths or Windows paths stored in the DB
- remote laptop access is a separate boundary from local filesystem access

If a change breaks one of these assumptions, treat it as an architectural change, not a local edit.

## Change Seams

This is the shortest path from feature type to code location.

### Add a new MCP capability
- add tool schema and dispatch in `custodian/mcp_server.py`
- add any new persistence to `custodian/schema.sql`
- expose in Admin only if the capability needs a local operator workflow

### Add a new Admin tab or operator panel
- add UI in `custodian/admin.py`
- add refresh/action methods in `custodian/admin.py`
- add any backing state in `custodian/schema.sql` if needed
- update widget/docs only after the code path works

### Add a new router-visible API or widget surface
- add endpoint/UI in `custodian/sandbox_router.py`
- add backing DB or subprocess logic in the same file or in `mcp_server.py`, depending on ownership
- if it should survive MCP restarts, prefer router/systemd ownership over MCP-thread ownership

### Add a new persisted feature
- start in `custodian/schema.sql`
- then update each consumer explicitly:
  `custodian/admin.py`, `custodian/mcp_server.py`, `custodian/sandbox_router.py`, `custodian/editor.py`

### Add a new remote-device capability
- decide first whether it belongs to:
  local SSH flow, router HTTP flow, or laptop-bridge MCP flow
- pairing/setup UX usually lives in `custodian/sandbox_router.py` + `bin/setup-device`
- remote file/command execution belongs in `laptop-bridge/server.py`

### Add a new status indicator
- decide whether it belongs in one or more of these:
  `/api/workbench`, router ticker, terminal title ticker, overlay ticker, notifier
- update all intended surfaces deliberately; status is currently split across multiple consumers

### Add a new project-launching workflow
- check `custodian/editor.py`
- check `bin/editor-session` and `bin/opencode-session`
- check path normalization assumptions before changing launch cwd logic

### Add a host boot/startup behavior
- update `config/start-workbench.vbs`
- if it should persist independently, consider a systemd user service with a matching installer in `bin/`
- make sure any Windows-specific path still works from the current clone location

## Blast Radius Guide

Use this before making edits to avoid accidental under-scoping.

### High blast radius files
- `custodian/mcp_server.py`
- `custodian/admin.py`
- `custodian/sandbox_router.py`
- `custodian/schema.sql`
- `config/start-workbench.vbs`

Changes here often affect multiple surfaces at once.

### Medium blast radius files
- `custodian/editor.py`
- `custodian/watchdog.py`
- `config/wave/widgets.json`
- `config/wave/widgets-laptop.json`
- `laptop-bridge/server.py`

### Lower blast radius files
- `custodian/sticky_notes.py`
- `custodian/ticker_overlay.py`
- `custodian/ticker_notifier.py`
- `bin/workbench-status`
- `bin/launch-ticker`

Lower blast radius does not mean low importance. It means behavior is more localized.

## Questions To Ask Before Changing Anything Big

Before major work, answer these first:
- which layer owns this change?
- does it need DB state?
- does it need an MCP tool, an Admin surface, a router endpoint, or more than one?
- does it need to survive MCP restarts?
- does it need to work from WSL, Windows-path projects, and remote devices?
- does it affect one status surface or all of them?

## Change Guidance

When adding or changing features, use this mental checklist:

### If the feature needs shared state
Ask:
- does it belong in `custodian.db`?
- which existing table is closest?
- which UIs and APIs will need to read it?

### If the feature needs AI/tool access
Ask:
- is this a new MCP tool?
- is it UI-only in Admin?
- is it router-only via HTTP?
- should it be available to Claude, OpenCode, or both?

### If the feature affects runtime visibility
Ask:
- should it appear in `/api/workbench`?
- should it appear in the terminal ticker?
- should it appear in the overlay ticker or notifier?

### If the feature affects remote devices
Ask:
- is it PC-local only?
- should the paired laptop see it?
- does it need an SSH path, HTTP path, or MCP path?

### If the feature affects paths or project launching
Ask:
- can the project path be Windows?
- can it be WSL?
- can it be remote on the laptop?

## Likely Future Pressure Points

These are the places most likely to need redesign as the system grows:
- shared DB helper duplication across `admin.py`, `editor.py`, `mcp_server.py`, and older modules
- overlap between Admin Editor tab and standalone Editor app
- overlap between router ticker, terminal ticker, overlay ticker, and notifier
- growing size and responsibility concentration inside `mcp_server.py`
- growing size and responsibility concentration inside `admin.py`

Those are not immediate blockers, but they are the main scaling seams.

## Working Summary

If you need one sentence to orient yourself:

NAI Workbench is a SQLite-backed orchestration layer for all projects and devices, with Wave-facing UIs on top and MCP as the main automation/control surface.

If you need one sentence to guide future changes:

Add features by deciding which layer owns them first: UX surface, control API, shared state, or host/runtime ops.
