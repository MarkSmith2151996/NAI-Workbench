# NAI Workbench — Laptop Setup Instructions

> **For Claude Code on the laptop**: Follow these steps exactly to deploy and
> configure the Custodian Admin TUI on the laptop's Wave Terminal. Every command
> includes expected output so you can verify each step.

---

## How Sync Works

The WSL path `/home/dev/projects/nai-workbench` is a **symlink** to
`/mnt/c/Users/Big A/NAI-Workbench` (the Windows checkout). This means:

- **PC edits** → instantly visible from the laptop (same physical files)
- **Laptop edits** (via admin TUI) → instantly visible on the PC
- **No git push/pull needed** between PC and laptop — they share one checkout
- **Git push** is only for backing up to GitHub (use the "Commit & Push" button in the Editor tab)

```
Laptop (Wave Terminal)
  └── SSH → WSL2 Ubuntu
        └── /home/dev/projects/nai-workbench (symlink)
              └── /mnt/c/Users/Big A/NAI-Workbench (actual files)
                    ├── Same files PC Claude Code edits
                    └── Git remote → GitHub (backup/versioning)
```

### Editor Tab Git Buttons

The Editor tab has two git buttons in the toolbar:

| Button | What it does |
|--------|-------------|
| **Commit & Push** | `git add -A` → `git commit` → `git push origin main` (one click) |
| **Pull** | `git pull origin main` (get changes from GitHub) |

The git status label updates automatically after: file saves, Claude edits, commits, and pulls.

## How to Update (After Dependency/Schema Changes Only)

> Since the symlink means PC and laptop share the same files, you usually don't
> need to update anything. Only run these if `requirements.txt` or `schema.sql`
> changed.

```bash
# SSH to the PC
ssh dev@100.95.20.98 -p 2222

# Update WSL-native venv
source ~/.custodian-venv/bin/activate
pip install -r /home/dev/projects/nai-workbench/custodian/requirements.txt

# Re-init DB (safe — only creates missing tables)
cd /home/dev/projects/nai-workbench
python custodian/init_db.py

# Restart the admin TUI
```

### If things go wrong — Nuclear Reset

```bash
# Fix the symlink if broken
rm -f /home/dev/projects/nai-workbench
ln -s '/mnt/c/Users/Big A/NAI-Workbench' /home/dev/projects/nai-workbench

# Recreate WSL venv from scratch
rm -rf ~/.custodian-venv
python3 -m venv ~/.custodian-venv
source ~/.custodian-venv/bin/activate
pip install -r /home/dev/projects/nai-workbench/custodian/requirements.txt

# Recreate database
cd /home/dev/projects/nai-workbench
rm -f custodian/custodian.db custodian/custodian.db-wal custodian/custodian.db-shm
python custodian/init_db.py

# Verify
python -c "import ast; ast.parse(open('custodian/admin.py').read()); print('SYNTAX OK')"
python custodian/admin.py  # press 'q' to quit
```

### Where code lives

| Location | What | Notes |
|----------|------|-------|
| `C:\Users\Big A\NAI-Workbench` | Windows checkout (actual files) | PC Claude Code edits here |
| `/home/dev/projects/nai-workbench` | WSL symlink → same files | Laptop admin TUI runs here |
| `~/.custodian-venv` | WSL-native Python venv | Separate from Windows .venv |
| GitHub `main` branch | Backup/versioning | "Commit & Push" button in Editor tab |

**Flow**: Both PC and laptop edit the same files (via symlink). "Commit & Push" backs up to GitHub.

---

## Overview: What You're Setting Up

The Admin TUI (ADMIN 01) is a 6-tab Textual application that runs inside Wave
Terminal. The **Editor tab** gives you a file browser + code editor + persistent
Claude Code session — Claude can Read, Edit, Write, Bash files and also query
the Custodian fossil system via MCP tools. Everything runs on the PC filesystem;
the laptop connects via Tailscale/SSH and edits the same files in real-time.

### System Architecture

```
Laptop (Wave Terminal)
  └── SSH via Tailscale → PC (Windows 11)
        └── bash bin/admin-session
              └── python custodian/admin.py   ← Textual TUI, 1826 lines
                    ├── [Projects]   Import from GitHub (clones to ~/projects/)
                    ├── [Custodian]  Index projects via Sonnet
                    ├── [Fossils]    Browse fossil history + details
                    ├── [Detective]  Pattern analysis (Sonnet/Opus)
                    ├── [Status]     DB stats, MCP query log
                    └── [Editor]     ← FILE EDITOR + CLAUDE CODE + GIT
                          ├── WorkbenchDirectoryTree (file browser, left 30 cols)
                          ├── TextArea (code editor, syntax highlighting, right)
                          ├── Git toolbar: [Commit & Push] [Pull] + status label
                          └── Claude Code chat (bottom panel)
                                ├── claude -p --session-id UUID --append-system-prompt ...
                                ├── Full tools: Read, Edit, Write, Bash, Glob, Grep
                                ├── Custodian MCP: get_project_fossil, lookup_symbol, etc.
                                ├── Tracks edited files → editor auto-reloads
                                └── Session persists across restarts (~/.custodian_claude_session)
```

### File Manifest (PC paths)

```
C:\Users\Big A\NAI-Workbench\
├── .claude/mcp.json                    # MCP server config for Claude Code (13 lines)
├── .gitignore                          # Ignores DB, venv, pycache (22 lines)
├── LAPTOP_SETUP.md                     # This file
├── config/mcp.json                     # Alternative MCP config (13 lines)
├── bin/
│   ├── admin-session                   # Widget entry point — activates venv + runs admin.py (22 lines)
│   └── custodian                       # CLI: index, admin, mcp, status, help (143 lines)
└── custodian/
    ├── admin.py                        # Textual TUI — 6 tabs, 1692 lines
    ├── mcp_server.py                   # MCP server — 8 tools, 573 lines
    ├── detective.py                    # Pattern analysis + prompt evolution, 383 lines
    ├── parse_symbols.py                # tree-sitter symbol extraction, 307 lines
    ├── store_fossil.py                 # Parse Sonnet JSON → SQLite, 175 lines
    ├── init_db.py                      # Create DB + seed projects + default prompt, 102 lines
    ├── index_project.sh                # Custodian pipeline orchestrator, 195 lines
    ├── setup.sh                        # Create venv + install + init DB, 80 lines
    ├── schema.sql                      # 6 tables + 5 indexes, 74 lines
    ├── requirements.txt                # mcp, tree-sitter, tree-sitter-languages, textual, rich
    ├── .venv/                          # Python 3.12 virtual environment
    └── custodian.db                    # SQLite WAL database (176 KB)
```

### Database Schema (6 tables)

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `projects` | Registered projects | name, path, stack, status, last_indexed |
| `fossils` | Versioned project snapshots | project_id, version, file_tree, architecture, summary |
| `symbols` | Function/class index | project_id, fossil_id, file_path, line_number, type, name, signature |
| `detective_insights` | Pattern analysis results | project_id, insight_type, content, model_used |
| `custodian_prompts` | Evolving prompts for Sonnet | project_id, prompt, created_by |
| `query_log` | MCP tool usage tracking | tool_name, project_name, query_params |

### Registered Projects

Projects are imported via **GitHub URL** in the Projects tab. New imports clone to
`~/projects/{repo-name}/` automatically. The seeded projects below have legacy
paths — they'll be replaced as you re-import from GitHub.

| Name | Path | Stack |
|------|------|-------|
| progress-tracker | (re-import from GitHub) | Next.js + React + Electron + Supabase + Zustand + react95 |
| finance95 | (re-import from GitHub) | Electron + Vite + React + @actual-app/api + Zustand |
| bjtrader | (re-import from GitHub) | Python + Textual + LangGraph + Claude CLI |
| fba-command-center | (re-import from GitHub) | Python + tkinter + SQLite |
| nai-workbench | `/home/dev/projects/nai-workbench` | Python + Textual + MCP + SQLite + tree-sitter |

---

## Step-by-Step Deploy (First Time Only)

> After first-time setup, see **"How to Update"** at the top of this doc.

### Step 1: SSH to PC from laptop

```bash
# Via Tailscale — connects to WSL2 Ubuntu through port 2222 → 2223 proxy
ssh dev@100.95.20.98 -p 2222
# OR if you have a Tailscale hostname alias:
ssh BigA-PC
```

**Expected**: You get a bash shell on WSL2 Ubuntu as `dev`.

### Step 2: Clone the repo (or verify it exists)

```bash
cd /home/dev/projects

# Create symlink to the Windows checkout (NOT a separate clone!)
# This makes PC and laptop edits instant — same physical files.
ln -s '/mnt/c/Users/Big A/NAI-Workbench' nai-workbench

# Verify the symlink works
ls nai-workbench/custodian/admin.py
```

**Expected**: File exists at the symlink target.

> **Why a symlink instead of a clone?** With a symlink, both PC Claude Code
> and the laptop admin TUI edit the same files. No git push/pull needed
> to sync between them.

### Step 3: Create WSL-native venv

The Windows `.venv` (with `Scripts/`) won't work in WSL. Create a separate
WSL-native venv at `~/.custodian-venv`:

```bash
python3 -m venv ~/.custodian-venv
source ~/.custodian-venv/bin/activate
pip install -r /home/dev/projects/nai-workbench/custodian/requirements.txt
```

**Verify imports**:
```bash
source ~/.custodian-venv/bin/activate
python -c "
from textual.widgets import DirectoryTree, TextArea, TabbedContent, RichLog
from textual.app import App
import mcp, tree_sitter, tree_sitter_languages
print('ALL IMPORTS OK')
"
```

**Expected**: `ALL IMPORTS OK`

### Step 4: Initialize the database

```bash
cd /home/dev/projects/nai-workbench
source ~/.custodian-venv/bin/activate

# Init DB (creates tables + seeds projects if DB doesn't exist)
python custodian/init_db.py

# Verify
python -c "
import sqlite3
conn = sqlite3.connect('custodian/custodian.db')
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
projects = conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
print(f'Tables: {sorted(tables)}')
print(f'Projects: {projects}')
conn.close()
"
```

**Expected**:
```
Tables: ['custodian_prompts', 'detective_insights', 'fossils', 'projects', 'query_log', 'symbols']
Projects: 5
```

### Step 5: Verify Claude CLI is available

```bash
which claude
claude --version
```

**Expected**: Path to claude binary + version (e.g., `2.1.49 (Claude Code)`).

If `claude` is not found, install Claude Code CLI:
```bash
npm install -g @anthropic-ai/claude-code
```

### Step 6: Verify admin.py loads without errors

```bash
cd /home/dev/projects/nai-workbench
source ~/.custodian-venv/bin/activate
python -c "import ast; ast.parse(open('custodian/admin.py').read()); print('SYNTAX OK')"
```

**Expected**: `SYNTAX OK`

### Step 7: Quick smoke test — launch and quit

```bash
source ~/.custodian-venv/bin/activate
python custodian/admin.py
```

**Expected**: Textual TUI appears with 6 tabs:
`[Projects] [Custodian] [Fossils] [Detective] [Status] [Editor]`

Press `q` to quit.

### Step 8: Verify MCP config exists

```bash
cat .claude/mcp.json
```

**Expected**: JSON with a `custodian` server entry pointing to `custodian/mcp_server.py`.

This makes the Custodian MCP tools available to any Claude Code session running
from the workbench directory — including the Editor tab's Claude chat.

---

## Configure Wave Terminal Widget on Laptop

### Option A: SSH widget (recommended)

1. Open Wave Terminal on the laptop
2. Create a new block / widget
3. Set the command to:

```bash
ssh dev@100.95.20.98 -p 2222 'cd /home/dev/projects/nai-workbench && bash bin/admin-session'
```

The `bin/admin-session` script automatically:
- Detects Windows vs Linux venv paths (Scripts/ vs bin/)
- Activates the venv
- Creates the DB if missing
- Launches `python custodian/admin.py`

4. Name the widget: **ADMIN 01**
5. Save and click to launch

### Option B: Direct execution (if filesystem is mounted)

If the PC's filesystem is mounted via Tailscale / SMB / SSHFS:

```bash
cd /path/to/mounted/NAI-Workbench
bash bin/admin-session
```

### Option C: From Claude Code on the laptop

If you're already in a Claude Code session on the laptop connected to the PC:

```bash
cd /home/dev/projects/nai-workbench
source ~/.custodian-venv/bin/activate
python custodian/admin.py
```

---

## How the Editor Tab Works (Detailed)

### File Browser (left panel)
- `WorkbenchDirectoryTree` rooted at the NAI-Workbench directory
- Filters out: `.git`, `node_modules`, `__pycache__`, `.venv`, `.next`, `dist`,
  `build`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `egg-info`
- Also hides: `.db`, `.db-wal`, `.db-shm`, `.pyc`, `.pyo`, `.lock`, images, fonts

### Code Editor (right panel)
- Textual `TextArea` with line numbers and Monokai theme
- Language auto-detection by extension:
  - `.py` → python
  - `.js`, `.ts`, `.tsx`, `.jsx` → javascript
  - `.json` → json, `.md` → markdown, `.css` → css
  - `.html` → html, `.sql` → sql, `.toml` → toml, `.yaml`/`.yml` → yaml
- **Save** button writes to disk (or auto-saves when switching files)
- **Reload** button discards edits and re-reads from disk

### Claude Code Chat (bottom panel)

#### Creating a session
1. Click **New Session** → generates UUID, saves to `~/.custodian_claude_session`
2. Chat shows: "Claude has full tool access — Read, Edit, Write, Bash, Glob, Grep + Custodian MCP tools"

#### How messages are sent
When you type a message and press Enter (or click Send):

1. `_build_claude_prompt(message)` constructs the full prompt:
   - If a file is open: prepends the file content (up to 2000 lines) in `<file>` tags
   - `_detect_project_for_file()` checks which registered project the file belongs to
   - Adds a hint: "Use get_project_fossil('project-name') for architecture context"
   - Appends the user's message

2. `_run_claude_query(prompt)` spawns the Claude CLI:
   ```
   claude -p \
     --output-format stream-json \
     --session-id <UUID> \
     --append-system-prompt <EDITOR_SYSTEM_PROMPT + fossil briefs>
   ```
   - `EDITOR_SYSTEM_PROMPT` (1335 chars) describes all 8 MCP tools
   - `_get_fossil_briefs()` queries SQLite for one-line summaries of all projects
   - Working directory = NAI-Workbench root → `.claude/mcp.json` is picked up
   - Environment cleaned: removes `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT`

3. Claude processes the request with full tool access:
   - Can call `get_project_fossil`, `lookup_symbol`, etc. via MCP
   - Can `Read`, `Edit`, `Write` files directly
   - Can run `Bash` commands
   - MCP queries are logged to `query_log` table

4. Response streams as NDJSON back to the chat log:
   - Text appears line-by-line
   - Tool calls shown with magenta `>>>` markers (e.g., `>>> Edit custodian/admin.py`)
   - Tool results shown dimmed
   - `_extract_text_from_event()` handles all event types:
     `content_block_delta`, `assistant.text`, `assistant.tool_use`, `tool_result`, `result`

5. After completion:
   - `_claude_edited_files` set lists all files Claude modified
   - Summary: "Files modified (N):" with relative paths
   - If the currently open file was edited → `_reload_current_file()` auto-refreshes
   - Cost and duration shown if available

#### Session persistence
- UUID saved to `~/.custodian_claude_session` (JSON: `{session_id, created_at}`)
- Claude CLI stores conversation history at `~/.claude/sessions/<UUID>/`
- Click **Resume** to reload a saved session → Claude remembers previous conversation
- Click **New Session** to start fresh (old session remains on disk)
- Session survives admin TUI restarts — the UUID is the persistent key

#### Stop button
- `_do_stop_claude()` calls `proc.terminate()` on the running subprocess
- Chat shows "[yellow]Stopped.[/yellow]"

---

## How the Fossil Integration Works

The Editor tab is NOT a standalone thing — it participates in the same custodian
architecture as all other tabs:

```
                    ┌──────────────────────────────────────┐
                    │           SQLite (custodian.db)       │
                    │  projects | fossils | symbols         │
                    │  detective_insights | query_log       │
                    │  custodian_prompts                    │
                    └─────┬──────┬──────┬──────┬───────────┘
                          │      │      │      │
         ┌────────────────┘      │      │      └──────────────┐
         │                       │      │                     │
         ▼                       ▼      ▼                     ▼
  ┌─────────────┐     ┌──────────┐  ┌──────────┐   ┌──────────────┐
  │ Custodian   │     │ Fossils  │  │Detective │   │ Editor Tab   │
  │ Tab         │     │ Tab      │  │ Tab      │   │              │
  │ (indexes)   │     │ (views)  │  │(analyzes)│   │ Claude -p    │
  └──────┬──────┘     └──────────┘  └─────┬────┘   │ + MCP tools  │
         │                                │        └──────┬───────┘
         ▼                                │               │
  ┌──────────────┐                        │               ▼
  │ Sonnet       │                        │        ┌──────────────┐
  │ (indexer)    │                        │        │ MCP Server   │
  │ creates      │                        │        │ (8 tools)    │
  │ fossils      │                        │        │ logs queries │
  └──────────────┘                        │        └──────┬───────┘
                                          │               │
                                          ▼               │
                                   ┌──────────────┐       │
                                   │ Detective    │◄──────┘
                                   │ reads query  │ (query_log tells detective
                                   │ log → refines│  what Claude actually needed)
                                   │ prompts      │
                                   └──────────────┘
```

1. **Editor Claude uses MCP** → queries `get_project_fossil`, `lookup_symbol`, etc.
2. **MCP server logs queries** → writes to `query_log` table
3. **Detective reads query_log** → sees what Claude asked for, identifies gaps
4. **Detective refines prompts** → writes improved custodian_prompts
5. **Next Custodian index** → uses refined prompt → better fossils
6. **Editor Claude benefits** → next session gets richer fossil data

This is the same feedback loop the rest of the system uses. The Editor tab just
adds another consumer of fossil data and another source of query_log entries.

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `e` | Switch to Editor tab |
| `p` | Switch to Projects tab |
| `i` | Switch to Custodian tab |
| `f` | Switch to Fossils tab |
| `d` | Switch to Detective tab |
| `s` | Switch to Status tab |
| `r` | Refresh all data tabs |
| `q` | Quit the admin TUI |
| `Enter` | Send message (when chat input is focused) |

---

## Troubleshooting

| Problem | Diagnosis | Fix |
|---------|-----------|-----|
| `ModuleNotFoundError: textual` | WSL venv not activated | `source ~/.custodian-venv/bin/activate && pip install -r custodian/requirements.txt` |
| `ModuleNotFoundError: mcp` | Same | Same |
| `python: command not found` (WSL) | Using Windows .venv from WSL | Use `~/.custodian-venv` not `custodian/.venv` |
| Symlink broken | `/home/dev/projects/nai-workbench` doesn't resolve | `ln -s '/mnt/c/Users/Big A/NAI-Workbench' /home/dev/projects/nai-workbench` |
| `claude: command not found` | Claude Code CLI not installed | `npm install -g @anthropic-ai/claude-code` |
| DB errors / "no such table" | DB not initialized or corrupted | Delete `custodian/custodian.db` then `python custodian/init_db.py` |
| Editor tree shows nothing | `_workbench_path` wrong | Verify symlink resolves: `ls /home/dev/projects/nai-workbench/custodian/` |
| Claude says "Create a session first" | No session created | Click "New Session" button |
| Claude doesn't use MCP tools | `.claude/mcp.json` missing or wrong cwd | Verify `.claude/mcp.json` exists in workbench root |
| Claude can't edit files | Permissions or pipe mode issue | Test: `echo "edit a test file" \| claude -p` from workbench dir |
| Session won't resume | Session file corrupted | Delete `~/.custodian_claude_session`, create new session |
| TUI crashes on launch | Python version or textual version | Need Python 3.10+ and textual >= 0.50.0 |
| tree-sitter FutureWarning | Benign deprecation warning | Ignore — does not affect functionality |
| `--append-system-prompt` flag unknown | Older Claude CLI version | Update: `npm update -g @anthropic-ai/claude-code` |
| Commit & Push fails | Git auth issue from WSL | Configure git credential helper: `git config credential.helper '/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe'` |

---

## Verified Test Results (2026-02-20)

All of these passed on the PC before writing this document:

| Test | Result |
|------|--------|
| Python 3.12.3 in venv | OK |
| textual 8.0.0 (DirectoryTree, TextArea, all widgets) | OK |
| rich, mcp, tree_sitter, tree_sitter_languages imports | OK |
| SQLite DB: 6 tables, 5 indexes, 5 projects, 2 fossils, 151 symbols, 1 prompt | OK |
| admin.py syntax (1826 lines) | OK |
| CustodianAdmin class loads, 8 keybindings, 18 editor/Claude methods | OK |
| EDITOR_SYSTEM_PROMPT: 1335 chars | OK |
| WorkbenchDirectoryTree: filters 12 dir patterns, 16 file extensions | OK |
| `_get_fossil_briefs()`: queries DB, returns summaries for all 5 projects | OK |
| `_detect_project_for_file()`: correctly maps files to all 5 projects | OK |
| Language detection: maps .py/.ts/.tsx/.js/.json/.md/.css/.html/.sql/.toml/.yaml | OK |
| Session file path: `~/.custodian_claude_session` | OK |
| MCP config: `.claude/mcp.json` exists and points to `custodian/mcp_server.py` | OK |
| mcp_server.py: loads, queries DB, `find_symbol` from `parse_symbols` works | OK |
| parse_symbols.py: extracts 21 symbols from admin.py, find_symbol finds by name | OK |
| detective.py: loads OK | OK |
| store_fossil.py: loads OK | OK |
| bin/admin-session: valid bash | OK |
| bin/custodian: valid bash | OK |
| index_project.sh: valid bash | OK |
| setup.sh: valid bash | OK |
| Claude CLI: found on PATH, version 2.1.49 | OK |
