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
| `custodian/services/tool_router.py` | Unified MCP/box/native-extension tool resolution layer |
| `custodian/services/windows_bridge.py` | WSL-native Windows command/file bridge with path conversion |
| `custodian/services/native.py` | Native extension HTTP client layer |
| `custodian/oauth_provider.py` | OAuth provider implementation for the HTTP MCP server |
| `custodian/schema.sql` | Canonical SQLite schema for Custodian state |
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
| 2026-06-01 | II-093 | Switched the Windows command bridge to PowerShell `-EncodedCommand` to fix quoting, spaced-path, and nested-command execution |
| 2026-06-01 | II-087 | Replaced the old HTTP Windows bridge with WSL-native Windows interop and path conversion |
| 2026-05-31 | EE-031 | Deployed the SearXNG-backed web search tool into the MCP surface |
| 2026-05-28 | II-083 | Added provider-aware LLM executor runtime support |
| 2026-05-27 | II-082 | Reused the existing LLM proxy fallback in the agent executor |
| 2026-05-27 | II-080 | Fixed prompt compiler handling for string `input_schema` values |
| 2026-05-27 | II-079 | Fixed agent executor `NoneType.items()` failure and improved traceback logging |
| 2026-05-27 | II-076 | Finalized integration architecture fixes across the runtime |
| 2026-03-15 | Repo update | Added Agent Factory MCP tools, Excalidraw widget, multi-device support, and expanded README |
| 2026-03-03 | Repo update | Added watchdog daemon, sandbox self-healing, and laptop connectivity tooling |

## Known Issues

- FP-029: Executor tool resolution still fails for some newly created agents despite the tools existing.
- FP-028: Pipeline layer still lacks async escalation and parallel dispatch primitives for richer watcher flows.
- FP-026: Agent executor can still produce no parseable OpenCode output after an LLM call.
- FP-024: New OpenCode sessions still lose time rediscovering project context and DB paths — this STATUS.md system is the intended fix.
- FP-023: Data-runner E2E remains blocked by cascading Mac infrastructure failures.
- FP-007: Hypothesis registration tasks can still fail when observations exist in reports but not as registered lake observation rows.
