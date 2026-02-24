#!/usr/bin/env python3
"""Custodian MCP Server — Exposes fossil data and live symbol queries to Claude.

8 tools:
- list_projects: All registered projects with status
- get_project_fossil: Latest architecture + summary + dependencies
- lookup_symbol: Live tree-sitter search (always-current line numbers)
- get_symbol_context: Sonnet's description + relationships from DB
- find_related_files: Files you'd touch to change a symbol
- get_recent_changes: Summarized recent commits
- get_detective_insights: Known patterns, warnings, coupling
- trigger_custodian: Run Sonnet indexing for a project
"""

import collections
import json
import os
import platform as _platform
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")

# Detect WSL and provide path translation
_IS_WSL = (_platform.system() == "Linux"
           and os.path.exists("/proc/version")
           and "microsoft" in open("/proc/version").read().lower())


def _to_native_path(path):
    """Convert Windows paths to WSL /mnt/ paths when running in WSL."""
    if not _IS_WSL or not path:
        return path
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path

# Import local symbol parser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_symbols import find_symbol


def get_db():
    """Get a SQLite connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def log_query(tool_name, project_name=None, params=None):
    """Log MCP tool usage for detective analysis."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO query_log (tool_name, project_name, query_params) VALUES (?, ?, ?)",
            (tool_name, project_name, json.dumps(params) if params else None),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Don't let logging failures break tool calls


def get_project_by_name(conn, name):
    """Look up a project by name (case-insensitive, partial match)."""
    # Try exact match first
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try case-insensitive
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try partial match
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'",
        (f"%{name}%",),
    ).fetchone()
    return row


# --- Sandbox State ---

_sandbox_proc = None
_sandbox_log = collections.deque(maxlen=5000)
_sandbox_project = None
_sandbox_command = None
_sandbox_port = None
_sandbox_log_lock = threading.Lock()
_sandbox_tmux_session = None
_sandbox_preview_type = None  # "web" or "terminal"
_sandbox_ttyd_proc = None     # ttyd process for terminal app preview
_sandbox_preview_url = None   # URL where the preview is accessible

WSH_PATH = "/home/dev/.waveterm/bin/wsh"
TTYD_PORT = 7681
ROUTER_PORT = 7777


def _ensure_sandbox_columns():
    """Migrate sandbox_state table to add preview_type and tmux_session columns."""
    try:
        conn = get_db()
        cols = [row[1] for row in conn.execute("PRAGMA table_info(sandbox_state)").fetchall()]
        if "preview_type" not in cols:
            conn.execute("ALTER TABLE sandbox_state ADD COLUMN preview_type TEXT")
        if "tmux_session" not in cols:
            conn.execute("ALTER TABLE sandbox_state ADD COLUMN tmux_session TEXT")
        if "preview_url" not in cols:
            conn.execute("ALTER TABLE sandbox_state ADD COLUMN preview_url TEXT")
        conn.commit()
        conn.close()
    except Exception:
        pass


_ensure_sandbox_columns()


# --- Sandbox Preview Router (port 7777) ---
# Separate file: sandbox_router.py — can run standalone or as background thread.
from sandbox_router import run_router as _run_sandbox_router
threading.Thread(target=_run_sandbox_router, daemon=True).start()


def _wsh_available():
    """Check if wsh is available (running inside Wave Terminal)."""
    return os.path.isfile(WSH_PATH) and os.environ.get("WAVETERM_BLOCKID")


def _open_wave_preview(app_type, port=None, tmux_session=None):
    """Open a preview block in Wave Terminal via wsh.

    The dedicated Sandbox widget (sidebar) handles the TUI controls.
    This just opens a quick preview block when called from MCP.
    """
    if not _wsh_available():
        return False
    try:
        if app_type == "web" and port:
            subprocess.run([WSH_PATH, "web", "open", "-m", f"http://localhost:{port}"],
                          capture_output=True, timeout=5)
        elif app_type == "terminal" and tmux_session:
            subprocess.run([WSH_PATH, "run", "-m", "--",
                           "tmux", "attach-session", "-t", tmux_session],
                          capture_output=True, timeout=5)
        else:
            return False
        return True
    except Exception:
        return False


def _sandbox_reader(proc):
    """Background thread: reads stdout+stderr into the ring buffer."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            for line in stream:
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                with _sandbox_log_lock:
                    _sandbox_log.append(decoded)
        except Exception:
            pass


def _tmux_log_reader(session_name, poll_interval=2):
    """Poll tmux pane content into the ring buffer for terminal apps."""
    import time
    last_len = 0
    while True:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-200"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                break
            lines = result.stdout.strip().split("\n")
            with _sandbox_log_lock:
                for line in lines[last_len:]:
                    if line.strip():
                        _sandbox_log.append(line)
            last_len = len(lines)
        except Exception:
            break
        time.sleep(poll_interval)


def _detect_sandbox_command(project_path):
    """Auto-detect the dev command for a project.

    Returns (command, port, app_type) where app_type is 'web' or 'terminal'.
    """
    pkg = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg) as f:
                data = json.load(f)
            scripts = data.get("scripts", {})
            if "dev" in scripts:
                return "npm run dev", 3000, "web"
            if "start" in scripts:
                return "npm start", 3000, "web"
        except (json.JSONDecodeError, OSError):
            pass

    # Use python3 on Linux/WSL, python on Windows
    py = "python3" if os.name != "nt" else "python"

    if os.path.isfile(os.path.join(project_path, "manage.py")):
        return f"{py} manage.py runserver", 8000, "web"

    for entry in ("app.py", "main.py"):
        fp = os.path.join(project_path, entry)
        if os.path.isfile(fp):
            try:
                with open(fp) as f:
                    content = f.read(2000)
                if any(kw in content for kw in ["textual", "curses", "blessed", "prompt_toolkit"]):
                    return f"{py} {entry}", None, "terminal"
                if any(kw in content for kw in ["flask", "Flask", "fastapi", "FastAPI", "uvicorn"]):
                    return f"{py} {entry}", 5000, "web"
            except OSError:
                pass
            return f"{py} {entry}", 5000, "web"

    return None, None, None


def _detect_test_command(project_path):
    """Auto-detect the test command for a project."""
    pkg = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg) as f:
                data = json.load(f)
            scripts = data.get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass

    if os.path.isfile(os.path.join(project_path, "pytest.ini")) or os.path.isfile(
        os.path.join(project_path, "pyproject.toml")
    ):
        return "pytest"

    if os.path.isdir(os.path.join(project_path, "tests")):
        return "pytest"

    return None


# --- MCP Server Setup ---

app = Server("custodian")


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_projects",
            description="List all registered projects with their status, stack, and last indexed time.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_project_fossil",
            description="Get the latest fossil (architecture summary, file tree, dependencies, known issues) for a project. This is the fastest way to understand a project's structure without exploring files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name (e.g., 'progress-tracker', 'finance95', 'bjtrader', 'fba-command-center')",
                    },
                    "include_file_tree": {
                        "type": "boolean",
                        "description": "Include the full file tree (can be large). Default: false.",
                        "default": False,
                    },
                    "include_symbols": {
                        "type": "boolean",
                        "description": "Include full symbol list from this fossil. Default: false.",
                        "default": False,
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="lookup_symbol",
            description="Find a function, class, component, or type by name using live tree-sitter parsing. Returns CURRENT file paths and line numbers (not from fossil — always accurate). Use this to find where something is defined.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for (partial match supported)",
                    },
                    "exact": {
                        "type": "boolean",
                        "description": "Exact name match only. Default: false.",
                        "default": False,
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="get_symbol_context",
            description="Get Sonnet's description and relationship analysis for a known symbol. Unlike lookup_symbol (which gives current location), this gives semantic understanding from the last fossil.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name (partial match supported)",
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="find_related_files",
            description="Given a symbol or concept, find all files that would likely need changes. Uses relationship data from fossils.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name or concept to find related files for",
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="get_recent_changes",
            description="Get summarized recent commits for a project (from the latest fossil).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="get_detective_insights",
            description="Get known patterns, warnings, coupling analysis, and architectural insights for a project (or cross-project insights if no project specified).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name. Omit for cross-project insights.",
                    },
                    "insight_type": {
                        "type": "string",
                        "description": "Filter by type: coupling, growth, pattern, regression, prompt_refinement",
                    },
                },
            },
        ),
        Tool(
            name="trigger_custodian",
            description="Run Sonnet indexing for a specific project. Creates a new fossil. This is an async operation — results won't be immediate.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name to index",
                    },
                },
                "required": ["project"],
            },
        ),
        # --- Sandbox tools ---
        Tool(
            name="sandbox_start",
            description=(
                "Start a sandbox process for a project. The sandbox runs inside a tmux session that the user's "
                "Sandbox widget auto-attaches to — the user SEES the program live in their terminal. "
                "IMPORTANT RULES: "
                "(1) When creating test programs, demos, or prototypes to DISPLAY in the sandbox, ALWAYS build "
                "terminal-based UIs using Python Textual, Rich, or curses — these render directly in the sandbox "
                "terminal and the user can see and interact with them immediately. NEVER create web servers (Flask, "
                "HTTP) for sandbox display — the user cannot see web pages in the terminal. "
                "(2) If the project needs Python packages (textual, rich, etc.), call sandbox_install FIRST. "
                "(3) Only use web mode (with port) for actual web projects (React, Next.js, Django) that already "
                "have a dev server — these will auto-open a Wave browser pane. "
                "(4) Do NOT pass a port unless the command actually starts a web server."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "command": {
                        "type": "string",
                        "description": "Override command (e.g., 'npm run dev', 'python app.py'). Auto-detected if omitted.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Override port (implies web app type). Auto-detected if omitted.",
                    },
                    "preview": {
                        "type": "boolean",
                        "description": "Open Wave Terminal preview pane (default true).",
                        "default": True,
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="sandbox_stop",
            description="Stop the currently running sandbox process.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="sandbox_restart",
            description="Restart the sandbox process (stop + start with same command).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="sandbox_status",
            description="Get the status of the sandbox process (running/stopped, PID, port, error count).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="sandbox_logs",
            description="Get recent sandbox output. Optionally filter to errors/warnings only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of lines to return (default 50).",
                        "default": 50,
                    },
                    "filter": {
                        "type": "string",
                        "description": "Filter: 'error', 'warning', or omit for all output.",
                    },
                },
            },
        ),
        Tool(
            name="sandbox_test",
            description="Run the project's test suite and return results. Auto-detects test command (npm test, pytest) or accepts an override.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Override test command. Auto-detected if omitted.",
                    },
                },
            },
        ),
        Tool(
            name="sandbox_install",
            description=(
                "Install dependencies for a sandbox project. "
                "IMPORTANT: Call this BEFORE sandbox_start if the project needs packages that aren't installed. "
                "For sandbox demos/prototypes, install 'textual' and 'rich' for terminal UIs (preferred over web frameworks). "
                "Accepts a list of packages (pip or npm), or auto-installs from requirements.txt / package.json. "
                "Uses pip3 for Python projects, npm for Node.js projects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific packages to install (e.g., ['textual', 'rich']). If omitted, installs from requirements.txt or package.json.",
                    },
                    "manager": {
                        "type": "string",
                        "enum": ["pip", "npm"],
                        "description": "Package manager to use. Auto-detected if omitted (pip for Python, npm for Node.js).",
                    },
                },
                "required": ["project"],
            },
        ),
        # --- Penpot tools ---
        Tool(
            name="penpot_list_projects",
            description="List all Penpot projects and their files (wireframes/designs).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="penpot_get_page",
            description="Get the structure of a Penpot file page — component names, layout frames, text content. Use to understand a wireframe design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Penpot file UUID (from penpot_list_projects).",
                    },
                    "page": {
                        "type": "string",
                        "description": "Page name to get (optional — returns all pages if omitted).",
                    },
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="penpot_export_svg",
            description="Export a Penpot page or frame as SVG. Claude can read SVG as XML to understand layouts and visual structure.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Penpot file UUID.",
                    },
                    "page": {
                        "type": "string",
                        "description": "Page name (optional — uses first page if omitted).",
                    },
                },
                "required": ["file_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "list_projects":
            return await handle_list_projects(arguments)
        elif name == "get_project_fossil":
            return await handle_get_fossil(arguments)
        elif name == "lookup_symbol":
            return await handle_lookup_symbol(arguments)
        elif name == "get_symbol_context":
            return await handle_get_symbol_context(arguments)
        elif name == "find_related_files":
            return await handle_find_related_files(arguments)
        elif name == "get_recent_changes":
            return await handle_get_recent_changes(arguments)
        elif name == "get_detective_insights":
            return await handle_get_detective_insights(arguments)
        elif name == "trigger_custodian":
            return await handle_trigger_custodian(arguments)
        elif name == "sandbox_start":
            return await handle_sandbox_start(arguments)
        elif name == "sandbox_stop":
            return await handle_sandbox_stop(arguments)
        elif name == "sandbox_restart":
            return await handle_sandbox_restart(arguments)
        elif name == "sandbox_status":
            return await handle_sandbox_status(arguments)
        elif name == "sandbox_logs":
            return await handle_sandbox_logs(arguments)
        elif name == "sandbox_test":
            return await handle_sandbox_test(arguments)
        elif name == "sandbox_install":
            return await handle_sandbox_install(arguments)
        elif name == "penpot_list_projects":
            return await handle_penpot_list_projects(arguments)
        elif name == "penpot_get_page":
            return await handle_penpot_get_page(arguments)
        elif name == "penpot_export_svg":
            return await handle_penpot_export_svg(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def handle_list_projects(args):
    log_query("list_projects")
    conn = get_db()
    rows = conn.execute(
        """SELECT p.name, p.path, p.stack, p.status, p.last_indexed,
                  COUNT(f.id) as fossil_count,
                  (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count
           FROM projects p
           LEFT JOIN fossils f ON f.project_id = p.id
           GROUP BY p.id
           ORDER BY p.name"""
    ).fetchall()
    conn.close()

    projects = []
    for row in rows:
        projects.append({
            "name": row["name"],
            "path": row["path"],
            "stack": row["stack"],
            "status": row["status"],
            "last_indexed": row["last_indexed"],
            "fossil_count": row["fossil_count"],
            "symbol_count": row["symbol_count"],
        })

    return [TextContent(type="text", text=json.dumps(projects, indent=2))]


async def handle_get_fossil(args):
    project_name = args["project"]
    include_tree = args.get("include_file_tree", False)
    include_symbols = args.get("include_symbols", False)

    log_query("get_project_fossil", project_name, args)
    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

    fossil = conn.execute(
        """SELECT * FROM fossils
           WHERE project_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (project["id"],),
    ).fetchone()

    if not fossil:
        conn.close()
        return [TextContent(type="text", text=f"No fossil found for '{project_name}'. Run trigger_custodian to create one.")]

    result = {
        "project": project["name"],
        "path": project["path"],
        "stack": project["stack"],
        "fossil_version": fossil["version"],
        "fossil_date": fossil["created_at"],
        "summary": fossil["summary"],
        "architecture": fossil["architecture"],
        "known_issues": fossil["known_issues"],
        "dependencies": fossil["dependencies"],
    }

    if include_tree:
        result["file_tree"] = fossil["file_tree"]

    if include_symbols:
        symbols = conn.execute(
            """SELECT file_path, line_number, type, name, signature, description, relationships
               FROM symbols WHERE fossil_id = ?
               ORDER BY file_path, line_number""",
            (fossil["id"],),
        ).fetchall()
        result["symbols"] = [dict(s) for s in symbols]

    conn.close()
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_lookup_symbol(args):
    """Live tree-sitter lookup — always-current line numbers."""
    project_name = args["project"]
    symbol_name = args["symbol"]
    exact = args.get("exact", False)

    log_query("lookup_symbol", project_name, {"symbol": symbol_name, "exact": exact})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    matches = find_symbol(project_path, symbol_name, exact=exact)

    if not matches:
        return [TextContent(type="text", text=f"No symbols matching '{symbol_name}' found in {project_name}.")]

    # Limit to 50 results
    if len(matches) > 50:
        matches = matches[:50]
        truncated = True
    else:
        truncated = False

    result = {"matches": matches, "count": len(matches)}
    if truncated:
        result["note"] = "Results truncated to 50. Use exact=true for precise matches."

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_get_symbol_context(args):
    """Get Sonnet's description and relationships from the fossil DB."""
    project_name = args["project"]
    symbol_name = args["symbol"]

    log_query("get_symbol_context", project_name, {"symbol": symbol_name})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Get latest fossil's symbols
    symbols = conn.execute(
        """SELECT s.file_path, s.line_number, s.type, s.name, s.signature,
                  s.description, s.relationships
           FROM symbols s
           JOIN fossils f ON f.id = s.fossil_id
           WHERE s.project_id = ?
             AND LOWER(s.name) LIKE LOWER(?)
           ORDER BY f.created_at DESC""",
        (project["id"], f"%{symbol_name}%"),
    ).fetchall()
    conn.close()

    if not symbols:
        return [TextContent(
            type="text",
            text=f"No symbol context for '{symbol_name}' in {project_name}. "
                 "The fossil may not include this symbol, or no fossil exists yet.",
        )]

    results = [dict(s) for s in symbols[:20]]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def handle_find_related_files(args):
    """Find files related to a symbol via relationship data."""
    project_name = args["project"]
    symbol_name = args["symbol"]

    log_query("find_related_files", project_name, {"symbol": symbol_name})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Get all symbols matching the name
    symbols = conn.execute(
        """SELECT s.file_path, s.name, s.relationships
           FROM symbols s
           JOIN fossils f ON f.id = s.fossil_id
           WHERE s.project_id = ?
             AND LOWER(s.name) LIKE LOWER(?)
           ORDER BY f.created_at DESC""",
        (project["id"], f"%{symbol_name}%"),
    ).fetchall()

    related_files = set()
    direct_files = set()

    for sym in symbols:
        direct_files.add(sym["file_path"])
        if sym["relationships"]:
            try:
                rels = json.loads(sym["relationships"])
                # Collect all referenced symbols
                referenced = set()
                for key in ("calls", "called_by", "depends_on"):
                    referenced.update(rels.get(key, []))

                # Look up file paths for referenced symbols
                for ref_name in referenced:
                    ref_rows = conn.execute(
                        """SELECT DISTINCT s.file_path
                           FROM symbols s
                           JOIN fossils f ON f.id = s.fossil_id
                           WHERE s.project_id = ? AND s.name = ?
                           ORDER BY f.created_at DESC""",
                        (project["id"], ref_name),
                    ).fetchall()
                    for r in ref_rows:
                        related_files.add(r["file_path"])
            except (json.JSONDecodeError, TypeError):
                pass

    conn.close()

    result = {
        "direct_files": sorted(direct_files),
        "related_files": sorted(related_files - direct_files),
        "all_files": sorted(direct_files | related_files),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_get_recent_changes(args):
    project_name = args["project"]
    log_query("get_recent_changes", project_name)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    fossil = conn.execute(
        "SELECT recent_changes, created_at FROM fossils WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    conn.close()

    if not fossil:
        return [TextContent(type="text", text=f"No fossil for '{project_name}'. Run trigger_custodian first.")]

    result = {
        "project": project_name,
        "fossil_date": fossil["created_at"],
        "recent_changes": fossil["recent_changes"],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_get_detective_insights(args):
    project_name = args.get("project")
    insight_type = args.get("insight_type")

    log_query("get_detective_insights", project_name, {"insight_type": insight_type})

    conn = get_db()

    query = "SELECT * FROM detective_insights WHERE 1=1"
    params = []

    if project_name:
        project = get_project_by_name(conn, project_name)
        if project:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project["id"])
    else:
        query += " AND project_id IS NULL"

    if insight_type:
        query += " AND insight_type = ?"
        params.append(insight_type)

    query += " ORDER BY created_at DESC LIMIT 20"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return [TextContent(type="text", text="No detective insights found.")]

    results = [dict(r) for r in rows]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def handle_trigger_custodian(args):
    project_name = args["project"]
    log_query("trigger_custodian", project_name)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Find the custodian CLI
    custodian_dir = os.path.dirname(os.path.abspath(__file__))
    index_script = os.path.join(custodian_dir, "index_project.sh")

    if not os.path.exists(index_script):
        return [TextContent(type="text", text=f"Custodian index script not found at {index_script}")]

    try:
        # Launch async — don't block the MCP call
        subprocess.Popen(
            ["bash", index_script, project["name"], _to_native_path(project["path"])],
            cwd=custodian_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return [TextContent(
            type="text",
            text=f"Custodian indexing started for '{project_name}'. "
                 "Use get_project_fossil in a minute to check results.",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start custodian: {e}")]


# --- Sandbox Handlers ---


async def handle_sandbox_start(args):
    global _sandbox_proc, _sandbox_project, _sandbox_command, _sandbox_port
    global _sandbox_tmux_session, _sandbox_preview_type

    project_name = args["project"]
    command_override = args.get("command")
    port_override = args.get("port")
    open_preview = args.get("preview", True)

    log_query("sandbox_start", project_name, args)

    # Stop existing sandbox if running
    if _sandbox_proc and _sandbox_proc.poll() is None:
        _sandbox_proc.terminate()
        try:
            _sandbox_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _sandbox_proc.kill()
    if _sandbox_tmux_session:
        subprocess.run(["tmux", "kill-session", "-t", _sandbox_tmux_session],
                      capture_output=True, timeout=5)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    if command_override:
        command = command_override
        port = port_override
        app_type = "web" if port else "terminal"
    else:
        command, port, app_type = _detect_sandbox_command(project_path)
        if not command:
            return [TextContent(
                type="text",
                text=f"Could not auto-detect dev command for '{project_name}'. "
                     "Pass a 'command' argument (e.g., 'npm run dev').",
            )]

    # Auto-install dependencies if manifest exists
    dep_msg = ""
    py = "python3" if os.name != "nt" else "python"
    req_file = os.path.join(project_path, "requirements.txt")
    pkg_file = os.path.join(project_path, "package.json")
    node_modules = os.path.join(project_path, "node_modules")

    if os.path.isfile(req_file):
        dep_result = subprocess.run(
            [py, "-m", "pip", "install", "-q", "--break-system-packages", "-r", "requirements.txt"],
            cwd=project_path, capture_output=True, text=True, timeout=120,
        )
        if dep_result.returncode == 0:
            dep_msg = " Deps installed from requirements.txt."
        else:
            dep_msg = f" WARNING: pip install failed: {dep_result.stderr[:200]}"
    elif os.path.isfile(pkg_file) and not os.path.isdir(node_modules):
        dep_result = subprocess.run(
            ["npm", "install"],
            cwd=project_path, capture_output=True, text=True, timeout=120,
        )
        if dep_result.returncode == 0:
            dep_msg = " Deps installed via npm install."
        else:
            dep_msg = f" WARNING: npm install failed: {dep_result.stderr[:200]}"

    _sandbox_log.clear()
    _sandbox_project = project_name
    _sandbox_command = command
    _sandbox_port = port
    _sandbox_preview_type = app_type

    try:
        global _sandbox_ttyd_proc, _sandbox_preview_url

        # Kill any previous ttyd
        if _sandbox_ttyd_proc:
            try:
                _sandbox_ttyd_proc.terminate()
                _sandbox_ttyd_proc.wait(timeout=3)
            except Exception:
                pass
            _sandbox_ttyd_proc = None

        # ALL sandboxes run in tmux for log capture + ttyd attachment
        session_name = f"sandbox-{project_name}"
        _sandbox_tmux_session = session_name

        # Kill any existing session with this name
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                      capture_output=True)

        if app_type == "terminal":
            # Short-lived scripts: wrap so session stays alive after exit
            tmux_cmd = f'{command}; echo "\\n[sandbox exited $?]"; sleep 86400'
        else:
            # Web servers run indefinitely, no wrapper needed
            tmux_cmd = command

        subprocess.run(["tmux", "new-session", "-d", "-s", session_name,
                       "-c", project_path, "bash", "-c", tmux_cmd],
                      capture_output=True)
        _sandbox_proc = None

        # Start log reader for Claude's sandbox_logs() tool
        threading.Thread(target=_tmux_log_reader, args=(session_name,), daemon=True).start()

        # Get tmux pane PID for tracking
        pid_result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(pid_result.stdout.strip()) if pid_result.returncode == 0 and pid_result.stdout.strip() else None

        # --- Preview URL ---
        # Web apps: point directly at the app's own URL (rendered HTML).
        # Terminal apps: use ttyd to wrap the tmux session as a web page.
        subprocess.run(["pkill", "-f", f"ttyd.*{TTYD_PORT}"], capture_output=True)
        import time
        time.sleep(0.3)

        if app_type == "web" and port:
            # Web app: iframe shows the actual rendered page
            preview_url = f"http://localhost:{port}"
            # Still start ttyd as fallback for terminal-style viewing
            _sandbox_ttyd_proc = subprocess.Popen(
                ["ttyd", "-p", str(TTYD_PORT), "-W",
                 "tmux", "attach-session", "-t", session_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # Terminal app: ttyd wraps the interactive TUI as a web page
            _sandbox_ttyd_proc = subprocess.Popen(
                ["ttyd", "-p", str(TTYD_PORT), "-W",
                 "tmux", "attach-session", "-t", session_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            preview_url = f"http://localhost:{TTYD_PORT}"

        _sandbox_preview_url = preview_url

        # Update DB
        conn = get_db()
        conn.execute("DELETE FROM sandbox_state WHERE project_id = ?", (project["id"],))
        conn.execute(
            """INSERT INTO sandbox_state
               (project_id, command, pid, port, status, preview_type, tmux_session, preview_url)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
            (project["id"], command, pid, port, app_type, session_name, preview_url),
        )
        conn.commit()
        conn.close()

        pid_info = f" (PID {pid})" if pid else ""
        port_info = f" on port {port}" if port else ""
        return [TextContent(
            type="text",
            text=f"Started `{command}` ({app_type}){port_info}{pid_info} for {project_name}.{dep_msg}"
                 f" Preview: {preview_url} — Sandbox widget (localhost:{ROUTER_PORT}) shows it automatically.",
        )]

    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start sandbox: {e}")]


async def handle_sandbox_stop(args):
    global _sandbox_proc, _sandbox_project, _sandbox_command, _sandbox_port
    global _sandbox_tmux_session, _sandbox_preview_type, _sandbox_ttyd_proc, _sandbox_preview_url

    log_query("sandbox_stop")

    is_running = (_sandbox_proc and _sandbox_proc.poll() is None) or _sandbox_tmux_session
    if not is_running:
        return [TextContent(type="text", text="No sandbox is running.")]

    try:
        # Kill ttyd preview server
        if _sandbox_ttyd_proc:
            try:
                _sandbox_ttyd_proc.terminate()
                _sandbox_ttyd_proc.wait(timeout=3)
            except Exception:
                pass
            _sandbox_ttyd_proc = None

        # Kill tmux session
        if _sandbox_tmux_session:
            subprocess.run(["tmux", "kill-session", "-t", _sandbox_tmux_session],
                          capture_output=True, timeout=5)

        # Terminate subprocess fallback
        if _sandbox_proc and _sandbox_proc.poll() is None:
            _sandbox_proc.terminate()
            try:
                _sandbox_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _sandbox_proc.kill()

        # Update DB
        if _sandbox_project:
            conn = get_db()
            project = get_project_by_name(conn, _sandbox_project)
            if project:
                conn.execute(
                    "UPDATE sandbox_state SET status = 'stopped', pid = NULL, tmux_session = NULL, preview_url = NULL WHERE project_id = ?",
                    (project["id"],),
                )
                conn.commit()
            conn.close()

        name = _sandbox_project or "unknown"
        _sandbox_proc = None
        _sandbox_project = None
        _sandbox_command = None
        _sandbox_port = None
        _sandbox_tmux_session = None
        _sandbox_preview_type = None
        _sandbox_ttyd_proc = None
        _sandbox_preview_url = None

        return [TextContent(type="text", text=f"Stopped sandbox for {name}.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error stopping sandbox: {e}")]


async def handle_sandbox_restart(args):
    log_query("sandbox_restart")

    if not _sandbox_project or not _sandbox_command:
        return [TextContent(type="text", text="No sandbox to restart. Use sandbox_start first.")]

    project_name = _sandbox_project
    command = _sandbox_command

    # Stop
    await handle_sandbox_stop({})

    # Start again with same settings
    return await handle_sandbox_start({"project": project_name, "command": command})


async def handle_sandbox_status(args):
    log_query("sandbox_status")

    is_running = (_sandbox_proc and _sandbox_proc.poll() is None) or _sandbox_tmux_session
    if not _sandbox_proc and not _sandbox_tmux_session:
        return [TextContent(type="text", text=json.dumps({
            "status": "stopped",
            "project": None,
            "command": None,
            "preview_type": None,
            "tmux_session": None,
        }))]

    if _sandbox_tmux_session:
        # Terminal app — check if tmux session is still alive
        check = subprocess.run(["tmux", "has-session", "-t", _sandbox_tmux_session],
                              capture_output=True, timeout=5)
        running = check.returncode == 0
        status = "running" if running else "exited"
        pid = None
    elif _sandbox_proc:
        running = _sandbox_proc.poll() is None
        status = "running" if running else f"exited (code {_sandbox_proc.returncode})"
        pid = _sandbox_proc.pid if running else None
    else:
        running = False
        status = "stopped"
        pid = None

    with _sandbox_log_lock:
        error_count = sum(
            1 for line in _sandbox_log
            if "error" in line.lower() and "warning" not in line.lower()
        )
        warning_count = sum(1 for line in _sandbox_log if "warning" in line.lower())
        log_lines = len(_sandbox_log)

    result = {
        "status": status,
        "project": _sandbox_project,
        "command": _sandbox_command,
        "pid": pid,
        "port": _sandbox_port,
        "preview_type": _sandbox_preview_type,
        "tmux_session": _sandbox_tmux_session,
        "preview_url": _sandbox_preview_url,
        "router_url": f"http://localhost:{ROUTER_PORT}",
        "log_lines": log_lines,
        "errors": error_count,
        "warnings": warning_count,
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_sandbox_logs(args):
    lines_count = args.get("lines", 50)
    log_filter = args.get("filter")

    log_query("sandbox_logs", _sandbox_project, args)

    with _sandbox_log_lock:
        all_lines = list(_sandbox_log)

    if log_filter == "error":
        all_lines = [l for l in all_lines if "error" in l.lower()]
    elif log_filter == "warning":
        all_lines = [l for l in all_lines if "warning" in l.lower()]

    tail = all_lines[-lines_count:]

    result = {
        "project": _sandbox_project,
        "total_lines": len(all_lines),
        "showing": len(tail),
        "filter": log_filter,
        "output": "\n".join(tail),
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_sandbox_test(args):
    command_override = args.get("command")

    log_query("sandbox_test", _sandbox_project, args)

    # Determine project path
    project_path = None
    if _sandbox_project:
        conn = get_db()
        project = get_project_by_name(conn, _sandbox_project)
        conn.close()
        if project:
            project_path = _to_native_path(project["path"])

    if not project_path:
        return [TextContent(
            type="text",
            text="No project context. Start a sandbox first or specify a project.",
        )]

    if command_override:
        command = command_override
    else:
        command = _detect_test_command(project_path)
        if not command:
            return [TextContent(
                type="text",
                text="Could not auto-detect test command. Pass a 'command' argument.",
            )]

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = {
            "command": command,
            "exit_code": result.returncode,
            "passed": result.returncode == 0,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Test command timed out after 120 seconds.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to run tests: {e}")]


async def handle_sandbox_install(args):
    """Install dependencies for a sandbox project."""
    project_name = args["project"]
    packages = args.get("packages", [])
    manager = args.get("manager")

    log_query("sandbox_install", project_name, args)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    # Auto-detect package manager if not specified
    if not manager:
        if os.path.isfile(os.path.join(project_path, "package.json")):
            manager = "npm"
        else:
            manager = "pip"

    py = "python3" if os.name != "nt" else "python"

    try:
        if packages:
            # Install specific packages
            if manager == "pip":
                cmd = [py, "-m", "pip", "install", "--break-system-packages"] + list(packages)
            else:
                cmd = ["npm", "install"] + list(packages)

            result = subprocess.run(
                cmd, cwd=project_path,
                capture_output=True, text=True, timeout=120,
            )
        else:
            # Auto-install from manifest files
            if manager == "pip":
                req_file = os.path.join(project_path, "requirements.txt")
                if os.path.isfile(req_file):
                    cmd = [py, "-m", "pip", "install", "--break-system-packages", "-r", "requirements.txt"]
                else:
                    return [TextContent(
                        type="text",
                        text=f"No requirements.txt found in {project_name}. "
                             "Pass specific packages: [\"textual\", \"rich\"]",
                    )]
                result = subprocess.run(
                    cmd, cwd=project_path,
                    capture_output=True, text=True, timeout=120,
                )
            else:
                cmd = ["npm", "install"]
                result = subprocess.run(
                    cmd, cwd=project_path,
                    capture_output=True, text=True, timeout=120,
                )

        # Build response
        output = {
            "project": project_name,
            "manager": manager,
            "packages": packages if packages else "from manifest",
            "exit_code": result.returncode,
            "success": result.returncode == 0,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Install timed out after 120 seconds.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to install: {e}")]


# --- Penpot Helpers ---

PENPOT_BASE = os.environ.get("PENPOT_URL", "http://localhost:9001")
PENPOT_EMAIL = os.environ.get("PENPOT_EMAIL", "admin@local.dev")
PENPOT_PASSWORD = os.environ.get("PENPOT_PASSWORD", "admin123")

_penpot_session = None


def _penpot_rpc(command, payload=None):
    """Call a Penpot RPC command. Auto-authenticates on first call."""
    global _penpot_session

    try:
        import requests
    except ImportError:
        raise RuntimeError("requests library required for Penpot tools: pip install requests")

    if _penpot_session is None:
        _penpot_session = requests.Session()
        resp = _penpot_session.post(
            f"{PENPOT_BASE}/api/rpc/command/login-with-password",
            json={"email": PENPOT_EMAIL, "password": PENPOT_PASSWORD},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            _penpot_session = None
            raise RuntimeError(f"Penpot login failed ({resp.status_code}): {resp.text[:200]}")

    resp = _penpot_session.post(
        f"{PENPOT_BASE}/api/rpc/command/{command}",
        json=payload or {},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        # Session may have expired — retry once
        _penpot_session = None
        return _penpot_rpc(command, payload)

    return resp.json()


def _extract_shape_info(shape):
    """Extract useful info from a Penpot shape object."""
    info = {
        "name": shape.get("name", ""),
        "type": shape.get("type", ""),
    }
    # Include text content if it's a text shape
    content = shape.get("content")
    if content and isinstance(content, dict):
        # Penpot text content is nested: content.children[].children[].text
        texts = []
        for para in content.get("children", []):
            for span in para.get("children", []):
                if "text" in span:
                    texts.append(span["text"])
        if texts:
            info["text"] = " ".join(texts)
    return info


# --- Penpot Handlers ---


async def handle_penpot_list_projects(args):
    log_query("penpot_list_projects")
    try:
        projects_data = _penpot_rpc("get-all-projects")
        results = []
        for proj in projects_data:
            proj_id = proj["id"]
            files = _penpot_rpc("get-project-files", {"project-id": proj_id})
            results.append({
                "id": proj_id,
                "name": proj.get("name", ""),
                "files": [
                    {"id": f["id"], "name": f.get("name", ""), "modified": f.get("modified-at", "")}
                    for f in files
                ],
            })
        return [TextContent(type="text", text=json.dumps(results, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Penpot error: {e}")]


async def handle_penpot_get_page(args):
    file_id = args["file_id"]
    page_name = args.get("page")

    log_query("penpot_get_page", None, args)
    try:
        file_data = _penpot_rpc("get-file", {"id": file_id})
        data = file_data.get("data", {})
        pages_index = data.get("pages-index", {})

        results = []
        for page_id, page_obj in pages_index.items():
            name = page_obj.get("name", "")
            if page_name and page_name.lower() != name.lower():
                continue

            objects = page_obj.get("objects", {})
            shapes = []
            for obj_id, shape in objects.items():
                info = _extract_shape_info(shape)
                if info["name"] or info.get("text"):
                    shapes.append(info)

            results.append({
                "page_id": page_id,
                "name": name,
                "shape_count": len(objects),
                "components": shapes[:100],  # Cap at 100 to avoid huge responses
            })

        if not results:
            return [TextContent(
                type="text",
                text=f"No pages found{' matching ' + repr(page_name) if page_name else ''}.",
            )]

        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=f"Penpot error: {e}")]


async def handle_penpot_export_svg(args):
    file_id = args["file_id"]
    page_name = args.get("page")

    log_query("penpot_export_svg", None, args)
    try:
        file_data = _penpot_rpc("get-file", {"id": file_id})
        data = file_data.get("data", {})
        pages_index = data.get("pages-index", {})

        # Find target page
        target_page_id = None
        target_page = None
        for page_id, page_obj in pages_index.items():
            name = page_obj.get("name", "")
            if page_name:
                if page_name.lower() == name.lower():
                    target_page_id = page_id
                    target_page = page_obj
                    break
            else:
                # Use first page
                target_page_id = page_id
                target_page = page_obj
                break

        if not target_page:
            return [TextContent(
                type="text",
                text=f"Page not found{' matching ' + repr(page_name) if page_name else ''}.",
            )]

        # Build a simplified SVG from the shape data
        objects = target_page.get("objects", {})
        svg_parts = ['<?xml version="1.0" encoding="UTF-8"?>']

        # Find the root frame to get dimensions
        root = objects.get("00000000-0000-0000-0000-000000000000", {})
        width = root.get("width", 1920)
        height = root.get("height", 1080)

        svg_parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        )

        for obj_id, shape in objects.items():
            stype = shape.get("type", "")
            name = shape.get("name", "")
            x = shape.get("x", 0)
            y = shape.get("y", 0)
            w = shape.get("width", 0)
            h = shape.get("height", 0)

            if stype == "frame":
                svg_parts.append(
                    f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" '
                    f'fill="none" stroke="#999" data-name="{name}"/>'
                )
            elif stype == "rect":
                fill = "#ccc"
                fills = shape.get("fills", [])
                if fills and isinstance(fills, list) and fills[0].get("color"):
                    fill = fills[0]["color"]
                svg_parts.append(
                    f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" '
                    f'fill="{fill}" data-name="{name}"/>'
                )
            elif stype == "text":
                info = _extract_shape_info(shape)
                text = info.get("text", name)
                svg_parts.append(
                    f'  <text x="{x}" y="{y + 16}" font-size="14" '
                    f'data-name="{name}">{text}</text>'
                )

        svg_parts.append("</svg>")
        svg_output = "\n".join(svg_parts)

        return [TextContent(type="text", text=svg_output)]

    except Exception as e:
        return [TextContent(type="text", text=f"Penpot error: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
