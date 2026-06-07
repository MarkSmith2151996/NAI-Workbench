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
| `custodian/tools/browser_use.py` | WSL-host browser-use MCP tool for LLM-driven browser automation, downloads, optional persistent Chromium profiles, default local Steel Browser sessions, legacy CDP attach mode, action-log reporting, isolated contexts, and Keepa session seeding |
| `custodian/tools/stock_quote.py` | MCP tool for fast yfinance-backed live quote lookups across one or more ticker symbols |
| `custodian/tools/stock_details.py` | MCP tool for single-ticker yfinance research details including valuation, earnings, analyst targets, and short interest |
| `custodian/tools/stock_history.py` | MCP tool for yfinance OHLCV price history with summary statistics and capped bar output |
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
| 2026-06-07 | II-121 | Verified the refreshed Keepa cookie JSON contains 11 cookies with long-lived token cookies and `cf_clearance`, but the MCP `browser_use` request failed at HTTP auth with `invalid_token` / `Authentication required`; per STOP rule the standalone Playwright backup was not run and `verification-results.txt` was written |
| 2026-06-07 | II-120 | Wired Steel `browser_use` sessions to create sessions with the Chrome 131 `userAgent`, normalize Steel WebSocket URLs to port 3010, inject 10 Keepa cookies from the shared cookie JSON before browser-use navigation, and verified the MCP path runs but still reaches Keepa Pro instead of the Product Finder grid |
| 2026-06-06 | II-119 | Retried Steel `keepa-auth` profile creation with credentials present, submitted the Keepa login form through Steel, but new-profile verification still hit `Keepa Pro | Unlock Advanced Data & Tools`; per STOP rule the MCP browser_use profile test was skipped and the login output log was updated |
| 2026-06-06 | II-118 | Attempted to create the Steel `keepa-auth` profile, but stopped before login because no Keepa credentials were found in `/tmp/.keepa_creds`, environment variables, or checked `.env` files; wrote the blocked login output log and confirmed Steel API/UI remained healthy |
| 2026-06-06 | II-117 | Stood up Steel Browser on WSL via split API/UI containers on alternate ports, patched the Steel fingerprint minVersion bug in-container, made `browser_use` default to Steel sessions with legacy CDP fallback, verified Keepa bypasses Cloudflare, and recorded that Keepa Product Finder still needs a Steel auth profile |
| 2026-06-06 | II-116 | Seeded isolated CDP browser contexts with Keepa cookies and origin localStorage from the default Chrome profile, added injection counts to `browser_use` results, and verified Keepa Product Finder Pro access in single and parallel isolated sessions |
| 2026-06-06 | II-115 | Added default-on CDP isolated browser contexts for `browser_use`, filtered CDP page visibility to per-call targets, disposed contexts during cleanup, and saved the updated tool copy to shared audit storage |
| 2026-06-05 | II-114 | Added browser-use action-log reporting, audited Keepa CDP navigation for 3 brands, wrote the optimized Keepa instruction template, and reran optimized timing comparisons |
| 2026-06-05 | II-113 | Installed `yfinance` in the Custodian venv and added `stock_quote`, `stock_details`, and `stock_history` MCP tools with hot-reload discovery and bad-ticker handling |
| 2026-06-05 | II-112 | Added `chrome_cdp` support to the `browser_use` MCP tool, verified browser-use uses `cdp_url`, confirmed Windows Chrome CDP reachability, and passed the SDK CDP example.com test through real Chrome |

## Known Issues

- FP-029: Executor tool resolution still fails for some newly created agents despite the tools existing.
- FP-028: Pipeline layer still lacks async escalation and parallel dispatch primitives for richer watcher flows.
- FP-026: Agent executor can still produce no parseable OpenCode output after an LLM call.
- FP-024: New OpenCode sessions still lose time rediscovering project context and DB paths — this STATUS.md system is the intended fix.
- FP-023: Data-runner E2E remains blocked by cascading Mac infrastructure failures.
- FP-007: Hypothesis registration tasks can still fail when observations exist in reports but not as registered lake observation rows.
- II-115/II-116 follow-up: isolated CDP context teardown can emit browser-use `StorageStateWatchdog` warnings after context disposal, though cleanup returns and isolation/Keepa verification passes.
- II-117 follow-up: local Steel Browser bypasses Cloudflare but Product Finder brand checks still hit Keepa login/subscription walls until a Steel `keepa-auth` profile is created via the viewer/manual-login flow.
- II-118 blocker: Steel `keepa-auth` profile creation cannot proceed until `KEEPA_USER` and `KEEPA_PASS` are provided, preferably in `/tmp/.keepa_creds` for task-scoped use.
- II-119 blocker: Programmatic Keepa login via Steel submitted credentials but did not produce a Product Finder-accessible `keepa-auth` profile; manual login via the Steel viewer at `http://localhost:5174` is likely required.
- II-120 blocker: Steel `browser_use` now injects the shared Keepa cookie JSON and MCP reports 10 injected cookies, but both MCP browser-use and direct Playwright checks still redirect Product Finder to `Keepa Pro | Unlock Advanced Data & Tools`; the shared cookie jar appears insufficient or stale for Product Finder access.
- II-121 blocker: The refreshed cookie file passed freshness checks, but the MCP HTTP test did not reach `browser_use` because the token lookup from `.env` files produced an unauthenticated request (`invalid_token` / `Authentication required`).
