# Custodian Editor — Full Implementation Plan

**Status:** Built — all 7 steps implemented, syntax verified
**Date:** 2026-02-23
**Context:** This was designed in a collaborative session. The goal is a dedicated project editor that connects to the existing custodian system (fossils, MCP, detective) and adds sandbox + Penpot integration.

---

## The Problem

When working on projects with Claude:
- 30-50% of usage is wasted on Claude re-exploring projects it already knew
- Sessions don't persist across restarts or devices (PC + Arch laptop via Tailscale)
- No way to test/run code from within the Claude session
- Wireframes in Penpot are disconnected from the coding workflow
- Too many Wave Terminal widgets (8), most redundant with the admin TUI

## The Solution

A **custodian-connected editor** that gives Claude full project context from turn 1, persistent sessions, sandbox control, and wireframe access — all through the existing MCP server.

---

## Architecture

```
┌─────────────────────┐         ┌──────────────────────────┐
│  Editor Launcher     │         │  Custodian MCP Server     │
│  (Textual TUI)       │         │  (mcp_server.py)          │
│                      │         │                           │
│  - Project picker    │         │  EXISTING (8 tools):      │
│  - Session manager   │         │  fossils, symbols,        │
│  - Fossil loader     │         │  insights, indexing        │
│  - exec → Claude CLI │         │                           │
└──────────┬──────────┘         │  + SANDBOX (6 tools):     │
           │                     │  start, stop, restart,    │
           │ exec replaces       │  status, logs, test       │
           ▼                     │                           │
┌─────────────────────┐         │  + PENPOT (3 tools):      │
│  Claude CLI          │◄──MCP──►│  list, get_page,          │
│  (full interactive)  │         │  export_svg               │
│                      │         │                           │
│  --session-id (DB)   │         └──────────┬───────────────┘
│  --mcp-config        │                    │
│  --append-system-prompt                   │ manages
│  cwd = project path  │                    ▼
└─────────────────────┘         ┌──────────────────────────┐
                                │  Sandbox Process          │
                                │  (npm run dev, pytest...) │
                                │  stdout/stderr → buffer   │
                                │  Wave pane tails output   │
                                └──────────────────────────┘
```

## What Already Exists

### Custodian System (`custodian/` directory)
- `admin.py` — 6-tab Textual TUI (Projects, Custodian, Fossils, Detective, Status, Editor)
- `mcp_server.py` — 8 MCP tools Claude can call
- `parse_symbols.py` — live tree-sitter symbol search
- `store_fossil.py` — saves Sonnet analysis to DB
- `detective.py` — cross-project analysis
- `init_db.py` — DB schema setup
- `index_project.sh` — Sonnet indexing pipeline

### MCP Config (`.claude/mcp.json`)
```json
{
  "mcpServers": {
    "custodian": {
      "command": "python",
      "args": ["custodian/mcp_server.py"],
      "cwd": "C:\\Users\\Big A\\NAI-Workbench",
      "env": { "PYTHONPATH": "C:\\Users\\Big A\\NAI-Workbench\\custodian" }
    }
  }
}
```

### Existing MCP Tools (8)
1. `list_projects()` — all registered projects with status
2. `get_project_fossil(project)` — architecture, file tree, deps, issues
3. `lookup_symbol(project, symbol)` — live tree-sitter search, current line numbers
4. `get_symbol_context(project, symbol)` — Sonnet's descriptions + relationships
5. `find_related_files(project, symbol)` — files to touch for a change
6. `get_recent_changes(project)` — summarized recent commits
7. `get_detective_insights(project?)` — patterns, warnings, coupling
8. `trigger_custodian(project)` — re-index with Sonnet

### System Prompt (`EDITOR_SYSTEM_PROMPT` in admin.py, lines 53-84)
Already teaches Claude the workflow: fossil first → symbols → edit → verify.
Reuse this in the new editor.

### Custodian DB (`custodian/custodian.db`)
Tables: projects, fossils, symbols, detective_insights, query_log, custodian_prompts

### Wave Terminal
- PC: `wsl://Ubuntu-24.04` connections
- Laptop: `ssh -t` via Tailscale to same WSL
- Penpot at localhost:9001

---

## Implementation Plan

### Step 1: DB Schema Updates (`init_db.py`)

Add two new tables:

```sql
CREATE TABLE IF NOT EXISTS editor_sessions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    session_id TEXT NOT NULL,
    summary TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    last_active TEXT DEFAULT (datetime('now')),
    device TEXT,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS sandbox_state (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    command TEXT,
    pid INTEGER,
    port INTEGER,
    status TEXT DEFAULT 'stopped',
    log_path TEXT
);
```

### Step 2: Sandbox MCP Tools (add to `mcp_server.py`)

6 new tools. The MCP server manages a background sandbox process:

```python
# Global state in mcp_server.py
_sandbox_proc = None
_sandbox_log = collections.deque(maxlen=5000)  # ring buffer
_sandbox_project = None
```

**Tools:**

| Tool | Args | Returns |
|------|------|---------|
| `sandbox_start(project, command?)` | project name, optional command override | "Started npm run dev on port 3000" |
| `sandbox_stop()` | none | "Stopped" |
| `sandbox_restart()` | none | "Restarted, compiled successfully" |
| `sandbox_status()` | none | "Running (PID 4521) on port 3000, 0 errors" |
| `sandbox_logs(lines?, filter?)` | line count, optional "error"/"warning" | Last N lines of stdout/stderr |
| `sandbox_test(command?)` | optional override (default: auto-detect) | "12 passed, 0 failed" or failure output |

**Auto-detection logic for `sandbox_start`:**
- `package.json` with `scripts.dev` → `npm run dev`
- `package.json` with `scripts.start` → `npm start`
- `app.py` or `main.py` → `python app.py`
- `manage.py` → `python manage.py runserver`

### Step 3: Penpot MCP Tools (add to `mcp_server.py`)

3 new tools. Uses Penpot REST API at `http://localhost:9001`:

| Tool | Args | Returns |
|------|------|---------|
| `penpot_list_projects()` | none | All Penpot projects/files |
| `penpot_get_page(file_id, page?)` | file ID, optional page name | Component names, layout structure, text content |
| `penpot_export_svg(file_id, page?)` | file ID, optional page | Raw SVG (Claude reads as XML) |

Penpot API auth: `admin@local.dev` / `admin123` (see `config/penpot/compose.env`)

### Step 4: Editor Launcher (`custodian/editor.py`)

New Textual app — the project picker + session manager.

**UI:**
```
┌─ CUSTODIAN EDITOR ──────────────────────────────────────────┐
│                                                              │
│  1) progress-tracker   Next.js+React    main   2 changed   │
│     Last: Feb 22 — "adding report card tab"                 │
│     Fossil: v3 (1 day old)  Symbols: 301                   │
│                                                              │
│  2) finance95           Electron+React  master  clean       │
│     Last: Feb 20 — "CSV import parsers"                     │
│     Fossil: v2 (3 days old)  Symbols: 89                   │
│                                                              │
│  [R] Resume   [N] New Session   [Q] Quit                    │
└──────────────────────────────────────────────────────────────┘
```

**On selection:**
1. Reads project from custodian DB
2. Loads fossil brief → builds system prompt (reuse `EDITOR_SYSTEM_PROMPT` + fossil data + sandbox tool docs)
3. Checks `editor_sessions` for existing session → offers Resume or New
4. Saves/updates session record in DB
5. `subprocess.run(["claude", ...])` — launches Claude as child process. When Claude exits (double-Esc), returns to the picker. With:
   - `--session-id <uuid>` (from DB)
   - `--resume` (if resuming)
   - `--mcp-config /path/to/.claude/mcp.json`
   - `--append-system-prompt <full context>`
   - `cwd` = project path

### Step 5: Launch Script (`bin/editor-session`)

```bash
#!/usr/bin/env bash
# Editor — Launch custodian editor with project picker
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKBENCH_DIR="$(dirname "$SCRIPT_DIR")"
WSL_VENV="$HOME/.custodian-venv"

if [ -d "$WSL_VENV/bin" ]; then
    source "$WSL_VENV/bin/activate"
fi

cd "$WORKBENCH_DIR"
python custodian/editor.py
```

### Step 6: Wave Config (replace 8 widgets → 4)

**Cut:** Dashboard, Claude CLI (old), Test Project, Komodo, Import Project, VS Code
**Keep:** Terminal (raw shell), Admin (custodian TUI), Penpot (wireframes)
**New:** Editor (custodian editor launcher)

Update `config/wave/widgets.json` and `config/wave/widgets-laptop.json`.

### Step 7: System Prompt Update

The system prompt for the editor session should include:
- `EDITOR_SYSTEM_PROMPT` (existing — workflow, fossil tools, rules)
- Fossil brief for the selected project
- Sandbox tool documentation
- Penpot tool documentation
- Session context (what you were working on last time, from `editor_sessions.summary`)

---

## Wave Terminal Layouts (3 workflow phases)

### Phase 1 — Session Start (4 quadrants)
```
┌──────────────────────┬───────────────────────┐
│  Editor (picker)     │  Admin TUI            │
│                      │  (status/fossils)     │
├──────────────────────┼───────────────────────┤
│  Penpot              │  Terminal             │
│  (wireframes)        │  (shell / sandbox)    │
└──────────────────────┴───────────────────────┘
```

### Phase 2 — Planning (2 columns)
```
┌──────────────────────┬───────────────────────┐
│  Editor              │  Penpot / Sandbox     │
│  (Claude planning)   │  (design reference    │
│                      │   or running app)     │
└──────────────────────┴───────────────────────┘
```

### Phase 3 — Coding (left split + right)
```
┌──────────────────────┬───────────────────────┐
│  Editor              │                       │
│  (Claude coding)     │  Sandbox              │
├──────────────────────┤  (live app, Claude    │
│  Terminal            │   controls via MCP)   │
│  (logs / git)        │                       │
└──────────────────────┴───────────────────────┘
```

---

## MCP Tool Summary (17 total)

```
KNOWLEDGE (8 existing):
  list_projects, get_project_fossil, lookup_symbol,
  get_symbol_context, find_related_files, get_recent_changes,
  get_detective_insights, trigger_custodian

SANDBOX (6 new):
  sandbox_start, sandbox_stop, sandbox_restart,
  sandbox_status, sandbox_logs, sandbox_test

PENPOT (3 new):
  penpot_list_projects, penpot_get_page, penpot_export_svg
```

---

## Changes Already Made (this session, 2026-02-23)

### admin.py — Project-scoped Editor tab
- Added `_editor_project_name` / `_editor_project_path` instance vars
- Project selector dropdown + "Open Project" button in Editor tab
- `_open_editor_project()` method — switches file tree, git, Claude to project
- Git operations use project path + auto-detect branch (not hardcoded `main`)
- Claude CLI runs with `cwd` set to project
- "Open in Editor" button on Projects tab cards
- Prompt builder injects explicit project context

### admin.py — Bug fixes
- Fossils + Detective tables: added `cursor_type = "row"` (was broken — detail panes never showed)
- Thread-safety: `_do_index_project()` and `_do_detective()` now capture Select values on main thread

### All changes syntax-verified and widget-tested (all 6 tabs, all 20 widgets load correctly)

### Full Editor System Built (2026-02-23, session 2)

**Step 1 — DB Schema:** Added `editor_sessions` and `sandbox_state` tables + indexes to `schema.sql`

**Step 2 — Sandbox MCP Tools (6):** Added to `mcp_server.py`:
- `sandbox_start(project, command?)` — auto-detects npm/python, background process + ring buffer
- `sandbox_stop()` / `sandbox_restart()` — process management
- `sandbox_status()` — PID, port, error/warning counts
- `sandbox_logs(lines?, filter?)` — tail with error/warning filter
- `sandbox_test(command?)` — runs test suite, returns pass/fail + output

**Step 3 — Penpot MCP Tools (3):** Added to `mcp_server.py`:
- `penpot_list_projects()` — lists all Penpot projects + files via RPC API
- `penpot_get_page(file_id, page?)` — shape names, text content, layout structure
- `penpot_export_svg(file_id, page?)` — reconstructs SVG from shape data
- Uses session-based auth with auto-login to `localhost:9001`

**Step 4 — Editor Launcher TUI:** Built `custodian/editor.py`:
- Project picker with keyboard navigation (j/k/arrows/enter)
- Shows fossil version + age, symbol count, git branch + dirty state
- Shows last session summary for resume
- Resume (R) or New (N) session — persists session IDs in DB
- Builds system prompt with fossil brief + all 17 MCP tool docs
- `os.execvp` into Claude CLI with `--session-id`, `--mcp-config`, `--append-system-prompt`

**Step 5 — Launch Script:** Built `bin/editor-session` (matches `admin-session` pattern)

**Step 6 — Wave Config:** Trimmed from 8 to 4 widgets:
- **Editor** (new) — `bin/editor-session`
- **Admin** — `bin/admin-session`
- **Penpot** — web widget at `:9001`
- **Terminal** — raw WSL shell
- Cut: Dashboard, Claude CLI (old), Test Project, Komodo, Import Project, VS Code

**MCP Server now has 17 tools total** (8 knowledge + 6 sandbox + 3 Penpot)

All files syntax-verified, schema validated (8 tables), JSON configs valid.

### Debugging Session (2026-02-23, session 2 continued)

**CRLF Bug (FIXED):** All files written from Windows had `\r\n` line endings. `bin/editor-session` failed with exit code 127 (`$'\r': command not found`). Fixed with `sed -i 's/\r$//'` on all files. Added `.gitattributes` with `eol=lf` for `bin/*` and `custodian/*.py`.

**WSL Path Translation (FIXED):** DB stores Windows paths (`C:\Users\...`) but WSL needs `/mnt/c/Users/...`. Added `_IS_WSL` detection + `_to_native_path()` to both `editor.py` and `admin.py`. Applied in `_open_editor_project()` and `ProjectCard.compose()`.

**Select.NULL Bug (FIXED):** In admin.py, `btn-open-project` handler checked `select.value != Select.BLANK` but the sentinel is `Select.NULL`. Changed to `isinstance(select.value, int)`.

**editor.py Launch Flow (FIXED):** Original code called `os.execvp` inside `self.exit()` callback — Textual's alternate screen buffer never cleaned up, causing ANSI garbage. Restructured: TUI returns launch info via `self.exit(result={...})`, `__main__` launches Claude AFTER `app.run()` returns. Added terminal reset escape codes before exec.

**Wave Widget Config (FIXED):**
- **REAL live config: `C:\Users\Big A\AppData\Roaming\waveterm\config\widgets.json`** (NOT `~/.config/waveterm/`)
- The `~/.config/waveterm/widgets.json` file exists but is NOT what Wave reads
- Root cause of "editor opens plain shell": we were updating the wrong config file
- The AppData config still had the old 9-widget layout with no Editor entry
- Fixed by writing the trimmed 4-widget config to the AppData location
- `controller: "cmd"` with `cmd: "bash -l /path/to/script"` is the correct pattern
- After updating: restart Wave Terminal or delete+recreate the widget pane

**Projects:** All 5 active (bjtrader reactivated), all paths accessible from WSL.

---

## Key File Paths
- Admin TUI: `custodian/admin.py` (~2360 lines)
- MCP server: `custodian/mcp_server.py` (17 tools)
- DB schema: `custodian/schema.sql` (8 tables)
- DB init: `custodian/init_db.py`
- Editor launcher: `custodian/editor.py`
- Editor launch script: `bin/editor-session`
- MCP config: `.claude/mcp.json`
- Wave widgets (PC): `config/wave/widgets.json` (4 widgets)
- Wave widgets (laptop): `config/wave/widgets-laptop.json` (4 widgets)
- This plan: `docs/EDITOR_PLAN.md`
