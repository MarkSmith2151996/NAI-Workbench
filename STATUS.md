<!-- USER-OWNED SECTION — edit freely, OpenCode preserves this -->

## About

Custodian: autonomous AI workflow infrastructure for LaManna Logistics.
MCP server + task system + agent factory + pipeline runtime + project management.
Built by Antonio (Tubs), planned in Claude Desktop, executed via OpenCode.

## Key Config

| Item | Value |
|------|-------|
| DB path | `custodian/custodian.db` |
| MCP server port | `8223` |
| MCP systemd service | `custodian-mcp-http` (user service) |
| Box container | `alpha-nai-workbench` |
| Box workspace mount | `/workspace/` |
| Shared folder | `/mnt/c/Users/Big A/custodian-shared/nai-workbench/` |
| Task prefix | `II` |
| GitHub repo | `MarkSmith2151996/nai-workbench` |

---

<!-- AUTO-MANAGED SECTION — updated by OpenCode after every task execution -->
<!-- Do NOT edit below this line manually. OpenCode maintains this. -->

## Architecture

- Top-level layout (1 level deep): `bin/` shell automation, `config/` deployment and widget configs, `custodian/` MCP/runtime core, `dashboard/` Textual UI assets, `docs/` runbooks, `hooks/` security hooks, `laptop-bridge/` remote Mac service, `shared/` outputs, `skills/` prompt skills, `templates/` starter files, `tests/` automated coverage, `tools/` root-level box tool workspace, `windows-bridge/` legacy bridge code, `wireframes/` design artifacts.
- Entry points: `custodian/mcp_server.py` starts the stdio MCP server, `custodian/mcp_http_server.py` serves the HTTP/OAuth MCP transport on port 8223, `custodian/admin.py` is the Admin TUI, `custodian/pipeline.py` runs YAML pipelines, `custodian/box_tool_server.py` serves `/workspace/tools` inside project boxes, and `custodian/watchdog.py` monitors sshd/Docker/sandbox state.
- Tool loading: `custodian/core/server.py` auto-discovers `custodian/tools/*.py`, validates `METADATA`, hot-reloads on file changes, and exposes them through stdio and HTTP transports; `custodian/services/tool_router.py` resolves among MCP tools, box tools, and native extensions.
- Data layer: `custodian/db/connection.py` and related `custodian/db/*` modules own the shared SQLite state (`custodian/custodian.db`) for projects, tasks, fossils, agents, memories, pipelines, tool registry, and query logs.
- Bridge layer: `custodian/services/windows_bridge.py` is now the WSL-native Windows interop path-conversion + command/file bridge; `custodian/services/laptop_*` and `laptop-bridge/` cover remote Mac access over Tailscale.
- Project sandboxes: `custodian/sandbox.py`, `custodian/sandbox_router.py`, and `custodian/box_bridge.py` manage Docker sandboxes, preview routing, and per-project box tool execution.
- Workstations: `custodian/services/workstations.py` owns spec-driven Docker workstation provisioning, warm instances, isolated and batch slot allocation, command execution, retirement, workstation-bound agent dispatch, and batch agent dispatch through `custodian/services/agent_loop.py`; MCP wrappers in `custodian/tools/workstation_*.py` expose lifecycle and dispatch.
- UI relationships: the Admin TUI (`custodian/admin.py`) is the operational control surface; Wave Terminal widgets and config under `config/wave/` point users into that TUI, Claude/OpenCode editor sessions, sandbox preview, and design tools.

## File Map

| File | Purpose |
|------|---------|
| `custodian/mcp_server.py` | Stdio MCP server entry point used by Claude/OpenCode sessions |
| `custodian/mcp_http_server.py` | Streamable HTTP + OAuth MCP server on port 8223 |
| `custodian/core/server.py` | Central MCP runtime, tool registry loader, hot-reload, and dispatch |
| `custodian/admin.py` | Main Textual Admin TUI with 10 tabs for projects, fossils, agents, sandboxes, devices, and ticker |
| `custodian/pipeline.py` | YAML pipeline parser/executor with tool, agent, foreach, watcher, and human gate steps |
| `custodian/box_tool_server.py` | Minimal HTTP tool server that loads `/workspace/tools/*.py` inside project boxes |
| `custodian/box_bridge.py` | Calls per-project box tool servers and manages box-level routing |
| `custodian/sandbox.py` | Docker sandbox lifecycle and process management |
| `custodian/sandbox_router.py` | Sandbox preview/status HTTP router on port 7777 |
| `custodian/watchdog.py` | Health watchdog for sshd, Docker, and stale sandbox cleanup |
| `custodian/db/connection.py` | Shared SQLite connection helpers and DB path constants |
| `custodian/db/shared.py` | Shared-folder IO helpers and cross-project utility DB operations |
| `custodian/db/tools_registry.py` | Box tool registration/update/reload plumbing and DB records |
| `custodian/services/workstations.py` | Workstation service for spec persistence, Docker lifecycle, slot allocation, batch dispatch, exec, retirement, and workstation agent dispatch |
| `custodian/services/agent_loop.py` | Container-local LLM tool-use loop that calls the OpenCode proxy and executes workstation tool commands |
| `custodian/services/tool_router.py` | Unified MCP/box/native-extension tool resolution layer |
| `custodian/services/windows_bridge.py` | WSL-native Windows command/file bridge with path conversion |
| `custodian/services/native.py` | Native extension HTTP client layer |
| `custodian/oauth_provider.py` | OAuth provider implementation for the HTTP MCP server |
| `custodian/schema.sql` | Canonical SQLite schema for Custodian state |
| `custodian/tools/workstation_*.py` | MCP wrappers for workstation create/update/status/allocate/release/retire/exec tools |
| `custodian/custodian.db` | Main shared SQLite DB used by tasks, projects, fossils, agents, tools, and memories |
| `config/wave/widgets.json` | Source-of-truth Wave widget config for the Windows PC |
| `config/wave/widgets-laptop.json` | Tailscale-aware Wave widget template for remote devices |
| `bin/install-watchdog` | Installs the watchdog as a user service |
| `README.md` | High-level system architecture, components, ports, and workflow overview |
| `CLAUDE.md` | Repo-specific operator context and architecture notes for Claude/OpenCode |
| `laptop-bridge/server.py` | Remote Mac bridge service exposed over Tailscale |

## Last 10 Changes

| Date | Task | What Changed |
|------|------|-------------|
| 2026-06-04 | II-103 | Backed up Custodian DB and OpenCode config to shared storage, committed/pushed backup-state repos, and wrote system backup manifest |
| 2026-06-03 | II-101 | Added separation-of-powers executor/planner guidance to global OpenCode instructions and generated Claude skill boundary update files |
| 2026-06-03 | II-100 | Added batch workstation dispatch, batch slot allocation/release, foreach shortcut routing for workstation agents, watcher slot queueing, and batch dispatch MCP wrapper |
| 2026-06-03 | II-099 | Added workstation-backed agent loop dispatch, agent/spec tool-definition integration, direct MCP dispatch, and single agent-step pipeline routing |
| 2026-06-03 | II-098 | Added Phase 1 workstation DB migrations, service module, MCP wrappers, and lifecycle verification report |
| 2026-06-03 | II-097 | Audited box, native extension, agent, pipeline, and sandbox runtime infrastructure for workstation layer planning; wrote recon report to shared output folder |
| 2026-06-02 | II-096 | Added Rule 17 to the global OpenCode instructions to block shared-folder writes when the target directory has not been pre-created |
| 2026-06-01 | II-093 | Switched the Windows command bridge to PowerShell `-EncodedCommand` to fix quoting, spaced-path, and nested-command execution |
| 2026-06-01 | II-087 | Replaced the old HTTP Windows bridge with WSL-native Windows interop and path conversion |
| 2026-05-31 | EE-031 | Deployed the SearXNG-backed web search tool into the MCP surface |

## Known Issues

- FP-029: Executor tool resolution still fails for some newly created agents despite the tools existing.
- FP-028: Pipeline layer still lacks async escalation and parallel dispatch primitives for richer watcher flows.
- FP-026: Agent executor can still produce no parseable OpenCode output after an LLM call.
- FP-024: New OpenCode sessions still lose time rediscovering project context and DB paths — this STATUS.md system is the intended fix.
- FP-023: Data-runner E2E remains blocked by cascading Mac infrastructure failures.
- FP-007: Hypothesis registration tasks can still fail when observations exist in reports but not as registered lake observation rows.
