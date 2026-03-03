#!/usr/bin/env python3
"""Custodian MCP Server — Exposes fossil data, live symbol queries, sandbox,
and Agent Factory management to Claude.

Knowledge (8): list_projects, get_project_fossil, lookup_symbol, get_symbol_context,
               find_related_files, get_recent_changes, get_detective_insights, trigger_custodian
Sandbox (7):   sandbox_start/stop/restart/status/logs/test/install, sandbox_exec
Agent (6):     agent_list, agent_create, agent_update, agent_delete, agent_run, agent_runs
Penpot (3):    penpot_list_projects, penpot_get_page, penpot_export_svg
Laptop (9):    laptop_read/write/edit_file, laptop_run_command, laptop_glob, laptop_grep,
               laptop_list_dir, laptop_system_info, laptop_download_file
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
from contextlib import contextmanager
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")

# Detect WSL and provide path translation
def _check_wsl():
    if _platform.system() != "Linux" or not os.path.exists("/proc/version"):
        return False
    with open("/proc/version") as f:
        return "microsoft" in f.read().lower()

_IS_WSL = _check_wsl()


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


@contextmanager
def db_connection():
    """Context manager for SQLite connections. Ensures cleanup on any exit path."""
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def log_query(tool_name, project_name=None, params=None):
    """Log MCP tool usage for detective analysis."""
    try:
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO query_log (tool_name, project_name, query_params) VALUES (?, ?, ?)",
                (tool_name, project_name, json.dumps(params) if params else None),
            )
            conn.commit()
    except Exception as e:
        print(f"[custodian] log_query failed: {e}", file=sys.stderr)


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


# --- Alpha Builds / Sandbox State (Docker-backed) ---
# The MCP sandbox tools now use Docker containers from the alpha_builds table.
# Each project gets an isolated container; commands run via `docker exec`.

_sandbox_log = collections.deque(maxlen=5000)
_sandbox_log_lock = threading.Lock()
_sandbox_project = None          # Currently active project name
_sandbox_container = None        # Currently active container name
_sandbox_command = None
_sandbox_port = None
_sandbox_state_lock = threading.Lock()   # Protects the 4 globals above
_log_reader_stop = threading.Event()     # Signals docker log reader to exit
_penpot_lock = threading.Lock()          # Protects _penpot_session creation

# Track background resources for graceful shutdown
_background_procs = []                   # Subprocess.Popen objects we've spawned
_background_procs_lock = threading.Lock()

ROUTER_PORT = 7777

# --- Sandbox Preview Router (port 7777) ---
try:
    from sandbox_router import run_router as _run_sandbox_router
    threading.Thread(target=_run_sandbox_router, daemon=True).start()
except ImportError:
    pass



def _docker_log_reader(container_name, stop_event):
    """Background thread: streams docker logs into the ring buffer.

    Checks *stop_event* between lines so the thread exits promptly when a new
    reader is started or the server shuts down.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "200", container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        with _background_procs_lock:
            _background_procs.append(proc)
        import select as _sel
        while not stop_event.is_set():
            # Use select to avoid blocking forever on readline
            ready, _, _ = _sel.select([proc.stdout], [], [], 1.0)
            if not ready:
                continue
            raw_line = proc.stdout.readline()
            if not raw_line:
                break  # EOF — container stopped
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            with _sandbox_log_lock:
                _sandbox_log.append(decoded)
    except Exception as e:
        if not stop_event.is_set():
            print(f"[custodian] docker log reader died: {e}", file=sys.stderr)
    finally:
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            with _background_procs_lock:
                try:
                    _background_procs.remove(proc)
                except ValueError:
                    pass


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
                if any(kw in content for kw in ["tkinter", "Tkinter", "pygame", "PyQt5",
                        "PyQt6", "PySide2", "PySide6", "wx", "gi.repository", "kivy",
                        "pyglet", "turtle"]):
                    return f"{py} {entry}", 8080, "gui"
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
                "NOTE: sandbox_start auto-installs from requirements.txt/package.json, so you only need this "
                "for extra packages not in the manifest. "
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
        # --- Sandbox exec (run any command in container, return output) ---
        Tool(
            name="sandbox_exec",
            description=(
                "Run a command inside the sandbox container and return stdout/stderr. "
                "Use this to diagnose crashes, check files, inspect state, or run "
                "one-off commands without starting/stopping the sandbox. "
                "The sandbox does NOT need to be running — only the container."
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
                        "description": "Command to run (e.g., 'python3 -c \"import fba_tui\"', 'cat /tmp/err.log', 'pip list')",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120)",
                    },
                },
                "required": ["project", "command"],
            },
        ),
        # --- Reindex request tool ---
        Tool(
            name="request_reindex",
            description=(
                "Request a fossil reindex for a project. Does NOT run immediately — "
                "creates a pending request the user must approve in the Admin TUI. "
                "Use when you notice a fossil is stale or missing information."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why reindexing is needed",
                    },
                },
                "required": ["project", "reason"],
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
        # --- Laptop Bridge tools (proxied to remote MCP server on laptop) ---
        Tool(
            name="laptop_read_file",
            description="Read a file from the laptop. Returns content with line numbers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path on the laptop"},
                    "offset": {"type": "integer", "description": "Start line (1-based). Default: 1."},
                    "limit": {"type": "integer", "description": "Max lines to return. Default: 2000."},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="laptop_write_file",
            description="Write/overwrite a file on the laptop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="laptop_edit_file",
            description="Exact string replacement in a file on the laptop (like Claude Code's Edit).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "old_string": {"type": "string", "description": "Text to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
        Tool(
            name="laptop_run_command",
            description="Execute a shell command on the laptop. Returns stdout, stderr, exit_code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Working directory (default: home)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)", "default": 120},
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="laptop_glob",
            description="Find files matching a glob pattern on the laptop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py')"},
                    "path": {"type": "string", "description": "Base directory (default: home)"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="laptop_grep",
            description="Search file contents with regex on the laptop (uses ripgrep if available, else grep).",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (default: home)"},
                    "glob_filter": {"type": "string", "description": "File glob filter (e.g., '*.py')"},
                    "context": {"type": "integer", "description": "Lines of context around matches (default: 0)"},
                    "max_results": {"type": "integer", "description": "Max results (default: 50)"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="laptop_list_dir",
            description="List directory contents with file types and sizes on the laptop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: home)"},
                },
            },
        ),
        Tool(
            name="laptop_system_info",
            description="Get laptop system info: OS, Python version, disk, memory, Tailscale status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="laptop_download_file",
            description=(
                "Download a file from the laptop to the PC. Use this for binary files "
                "(images, archives, etc.) that can't transfer cleanly through JSON text. "
                "The file is saved to the specified local_path on the PC."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute path on the laptop (e.g., '/home/LaManna/screenshot.png')",
                    },
                    "local_path": {
                        "type": "string",
                        "description": "Absolute path to save on the PC (e.g., '/tmp/screenshot.png')",
                    },
                },
                "required": ["remote_path", "local_path"],
            },
        ),
        # ── Agent Factory Tools ──────────────────────────────────────────
        Tool(
            name="agent_list",
            description=(
                "List all agents in the Agent Factory. Returns name, model, project, "
                "description, and recent run count for each agent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: 'active' (default) or 'deleted'.",
                        "default": "active",
                    },
                },
            },
        ),
        Tool(
            name="agent_create",
            description=(
                "Create a new agent in the Agent Factory. Agents are stored in the shared "
                "Workbench database and can be run from any Claude session or the Admin TUI. "
                "At minimum provide a name and system_prompt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique agent name (e.g., 'code-reviewer', 'test-writer')",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "The system prompt that defines the agent's behavior and expertise",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short human-readable description of what the agent does",
                    },
                    "model": {
                        "type": "string",
                        "description": "Claude model: 'sonnet' (default), 'opus', or 'haiku'",
                        "default": "sonnet",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name to bind to (optional — sets working directory when running)",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Max agentic turns (default 20)",
                        "default": 20,
                    },
                },
                "required": ["name", "system_prompt"],
            },
        ),
        Tool(
            name="agent_update",
            description=(
                "Update an existing agent's configuration. Pass the agent name or ID "
                "and any fields to change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name or ID to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name (optional)",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "New system prompt (optional)",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                    "model": {
                        "type": "string",
                        "description": "New model: 'sonnet', 'opus', or 'haiku' (optional)",
                    },
                    "project": {
                        "type": "string",
                        "description": "New project binding (optional, empty string to unbind)",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "New max turns (optional)",
                    },
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="agent_delete",
            description="Delete an agent by name or ID (soft-delete — sets status to 'deleted').",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name or ID to delete",
                    },
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="agent_run",
            description=(
                "Run an agent via Claude CLI and return the result. The agent runs as a "
                "subprocess with its configured model, system prompt, and project context. "
                "Pass an optional 'prompt' to override the default starter prompt. "
                "Returns the agent's output text, token usage, and cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name or ID to run",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Task/prompt to send to the agent (overrides default)",
                    },
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="agent_runs",
            description=(
                "Get run history for agents. Returns recent runs with status, tokens, "
                "cost, and output summary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name or ID to filter by (optional — all agents if omitted)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max runs to return (default 10)",
                        "default": 10,
                    },
                },
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
        elif name == "sandbox_exec":
            return await handle_sandbox_exec(arguments)
        elif name == "request_reindex":
            return await handle_request_reindex(arguments)
        elif name == "penpot_list_projects":
            return await handle_penpot_list_projects(arguments)
        elif name == "penpot_get_page":
            return await handle_penpot_get_page(arguments)
        elif name == "penpot_export_svg":
            return await handle_penpot_export_svg(arguments)
        elif name == "laptop_download_file":
            return await handle_laptop_download(arguments)
        elif name.startswith("laptop_"):
            return await handle_laptop_bridge(name, arguments)
        # Agent Factory
        elif name == "agent_list":
            return await handle_agent_list(arguments)
        elif name == "agent_create":
            return await handle_agent_create(arguments)
        elif name == "agent_update":
            return await handle_agent_update(arguments)
        elif name == "agent_delete":
            return await handle_agent_delete(arguments)
        elif name == "agent_run":
            return await handle_agent_run(arguments)
        elif name == "agent_runs":
            return await handle_agent_runs(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def handle_list_projects(args):
    log_query("list_projects")
    with db_connection() as conn:
        rows = conn.execute(
            """SELECT p.name, p.path, p.stack, p.status, p.last_indexed,
                      COUNT(f.id) as fossil_count,
                      (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               GROUP BY p.id
               ORDER BY p.name"""
        ).fetchall()

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
    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

        fossil = conn.execute(
            """SELECT * FROM fossils
               WHERE project_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (project["id"],),
        ).fetchone()

        if not fossil:
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

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_lookup_symbol(args):
    """Live tree-sitter lookup — always-current line numbers."""
    project_name = args["project"]
    symbol_name = args["symbol"]
    exact = args.get("exact", False)

    log_query("lookup_symbol", project_name, {"symbol": symbol_name, "exact": exact})

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

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

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
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

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
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

    result = {
        "direct_files": sorted(direct_files),
        "related_files": sorted(related_files - direct_files),
        "all_files": sorted(direct_files | related_files),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_get_recent_changes(args):
    project_name = args["project"]
    log_query("get_recent_changes", project_name)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        fossil = conn.execute(
            "SELECT recent_changes, created_at FROM fossils WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            (project["id"],),
        ).fetchone()

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

    with db_connection() as conn:
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

    if not rows:
        return [TextContent(type="text", text="No detective insights found.")]

    results = [dict(r) for r in rows]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def handle_trigger_custodian(args):
    project_name = args["project"]
    log_query("trigger_custodian", project_name)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Find the custodian CLI
    custodian_dir = os.path.dirname(os.path.abspath(__file__))
    index_script = os.path.join(custodian_dir, "index_project.sh")

    if not os.path.exists(index_script):
        return [TextContent(type="text", text=f"Custodian index script not found at {index_script}")]

    try:
        # Create indexing run record + log file
        log_path = f"/tmp/custodian/indexing-{project['name']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        with db_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO indexing_runs (project_id, status, log_path, started_at)
                   VALUES (?, 'running', ?, datetime('now'))""",
                (project["id"], log_path),
            )
            run_id = cursor.lastrowid
            conn.commit()

        # Launch async — redirect output to log file instead of DEVNULL
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            ["bash", index_script, project["name"], _to_native_path(project["path"]), str(run_id)],
            cwd=custodian_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        # Close Python's copy of the FD — the subprocess owns it now
        log_file.close()
        with _background_procs_lock:
            _background_procs.append(proc)
        return [TextContent(
            type="text",
            text=f"Custodian indexing started for '{project_name}' (run #{run_id}). "
                 f"Log: {log_path}\n"
                 "Use get_project_fossil in a minute to check results.",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start custodian: {e}")]


# --- Reindex Request Handler ---


async def handle_request_reindex(args):
    """Create a pending reindex request for user approval in Admin TUI."""
    project_name = args.get("project", "")
    reason = args.get("reason", "")
    log_query("request_reindex", project_name, args)

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]
    if not reason:
        return [TextContent(type="text", text="Error: 'reason' is required.")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        conn.execute(
            """INSERT INTO reindex_requests (project_id, requested_by, reason, status)
               VALUES (?, ?, ?, 'pending')""",
            (project["id"], "claude", reason),
        )
        conn.commit()

    return [TextContent(
        type="text",
        text=f"Reindex request created for '{project['name']}'. Awaiting user approval in Admin TUI.",
    )]


# --- Sandbox Handlers (Docker-backed via Alpha Builds) ---
# All sandbox operations now run inside Docker containers.
# Container naming: alpha-{project_name}


def _pick_image(project_name, project_path, stack=""):
    """Decide the Docker image based on devcontainer or stack detection."""
    devcontainer_path = os.path.join(project_path, ".devcontainer")
    if os.path.isdir(devcontainer_path):
        dockerfile = os.path.join(devcontainer_path, "Dockerfile")
        if os.path.isfile(dockerfile):
            image_name = f"alpha-{project_name}:latest"
            subprocess.run(
                ["docker", "build", "-t", image_name, "-f", dockerfile, project_path],
                capture_output=True, text=True, timeout=300,
            )
            return image_name
    if "Python" in stack:
        return "python:3.12"
    elif any(s in stack for s in ("Node", "React", "Next", "Electron")):
        return "node:22"
    return "python:3.12"


def _auto_install_deps(container_name, project_path):
    """Auto-install dependencies from requirements.txt or package.json if present.

    Also ensures tmux is available (needed for shared terminal sessions).
    Runs silently — skips if deps are already satisfied.  Called by
    handle_sandbox_start before launching the user command.
    """
    try:
        # Ensure tmux is installed (needed for ttyd shared sessions)
        subprocess.run(
            ["docker", "exec", container_name,
             "bash", "-c", "which tmux >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq tmux >/dev/null 2>&1)"],
            capture_output=True, text=True, timeout=60,
        )

        req_txt = os.path.join(project_path, "requirements.txt")
        pkg_json = os.path.join(project_path, "package.json")

        if os.path.isfile(req_txt):
            # pip install -r requirements.txt (quiet, skips already-installed)
            print(f"[sandbox] Auto-installing pip deps for {container_name}…",
                  file=sys.stderr)
            subprocess.run(
                ["docker", "exec", "-w", "/workspace", container_name,
                 "bash", "-c", "pip install -q -r requirements.txt 2>/dev/null"],
                capture_output=True, text=True, timeout=120,
            )
        elif os.path.isfile(pkg_json):
            print(f"[sandbox] Auto-installing npm deps for {container_name}…",
                  file=sys.stderr)
            subprocess.run(
                ["docker", "exec", "-w", "/workspace", container_name,
                 "bash", "-c", "npm install --silent 2>/dev/null"],
                capture_output=True, text=True, timeout=120,
            )
    except Exception as e:
        # Never fail the sandbox start over a dep install issue
        print(f"[sandbox] Auto-install warning: {e}", file=sys.stderr)


def _get_or_create_container(project_name, project_path, stack="", port=None):
    """Get existing alpha build container, or create one.

    If *port* is given and the existing container doesn't map that port,
    the container is recreated with the port exposed.
    """
    container_name = f"alpha-{project_name}"

    # Check if container exists
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True,
    )
    container_exists = result.returncode == 0

    # If port requested, verify the existing container has it mapped
    if container_exists and port:
        port_check = subprocess.run(
            ["docker", "port", container_name, str(port)],
            capture_output=True, text=True,
        )
        if port_check.returncode != 0 or not port_check.stdout.strip():
            # Port not mapped — must recreate
            subprocess.run(["docker", "rm", "-f", container_name],
                           capture_output=True, text=True, timeout=30)
            container_exists = False

    if container_exists:
        is_running = result.stdout.strip() == "true"
        if not is_running:
            subprocess.run(["docker", "start", container_name], capture_output=True)
        return container_name

    # Container doesn't exist — create one
    image_name = _pick_image(project_name, project_path, stack)

    run_cmd = [
        "docker", "run", "-d", "--name", container_name,
        "-v", f"{project_path}:/workspace", "-w", "/workspace",
    ]
    if port:
        run_cmd += ["-p", f"{port}:{port}"]
    run_cmd += [image_name, "sleep", "infinity"]

    subprocess.run(run_cmd, capture_output=True, text=True, timeout=60)

    # Ensure ttyd is available inside the container (for terminal-in-browser)
    # Use specific version URL — /latest/ redirect can serve HTML instead of binary
    subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         "which ttyd >/dev/null 2>&1 && ttyd --version >/dev/null 2>&1 || "
         "(curl -sL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 "
         "-o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd)"],
        capture_output=True, text=True, timeout=120,
    )

    # Auto-install project dependencies on first container creation
    # Search inside the container (avoids host/container path mismatch)
    find_req = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         "find /workspace -maxdepth 2 -name requirements.txt -type f | head -1"],
        capture_output=True, text=True, timeout=10,
    )
    req_path = find_req.stdout.strip() if find_req.returncode == 0 else ""

    if req_path:
        subprocess.run(
            ["docker", "exec", container_name,
             "pip", "install", "-q", "-r", req_path],
            capture_output=True, text=True, timeout=300,
        )
    else:
        # Check for package.json (Node projects)
        check_pkg = subprocess.run(
            ["docker", "exec", container_name, "test", "-f", "/workspace/package.json"],
            capture_output=True, text=True,
        )
        if check_pkg.returncode == 0:
            subprocess.run(
                ["docker", "exec", "-w", "/workspace", container_name,
                 "npm", "install", "--silent"],
                capture_output=True, text=True, timeout=300,
            )

    # Save to alpha_builds table
    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if project:
            cid_result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", container_name],
                capture_output=True, text=True,
            )
            cid = cid_result.stdout.strip()[:12] if cid_result.returncode == 0 else ""
            ports_json = json.dumps({str(port): str(port)}) if port else "{}"
            conn.execute(
                "DELETE FROM alpha_builds WHERE project_id = ?", (project["id"],)
            )
            conn.execute(
                """INSERT INTO alpha_builds (project_id, container_id, container_name,
                   image, status, ports, started_at) VALUES (?, ?, ?, ?, 'running', ?, datetime('now'))""",
                (project["id"], cid, container_name, image_name, ports_json),
            )
            conn.commit()

    return container_name


WEB_HINTS = [
    "http.server", "HTTPServer", "flask", "fastapi", "uvicorn",
    "npm run dev", "npm start", "next dev", "vite", "manage.py runserver",
    "gunicorn", "express", "serve", "webpack-dev-server", "streamlit",
]


def _is_web_command(command):
    """Check if a command is a web server (serves HTTP natively)."""
    return any(hint in command for hint in WEB_HINTS)


GUI_HINTS = [
    "tkinter", "Tkinter", "pygame", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "wx", "gtk", "gi.repository", "kivy", "pyglet", "turtle",
]


def _is_gui_command(command):
    """Check if a command needs an X display (desktop GUI app)."""
    return any(hint in command for hint in GUI_HINTS)


SANDBOX_VNC_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Sandbox</title>
<style>
*{margin:0;padding:0}
html,body{width:100%;height:100%;overflow:hidden;background:#1e1e2e}
#screen{width:100vw;height:100vh}
#screen canvas{width:100%!important;height:100%!important}
</style>
<script type="module">
import RFB from './core/rfb.js';
const host=window.location.hostname,port=window.location.port;
const proto=window.location.protocol==='https:'?'wss':'ws';
const url=proto+'://'+host+':'+port+'/websockify';
const rfb=new RFB(document.getElementById('screen'),url);
rfb.scaleViewport=true;rfb.resizeSession=true;
</script>
</head><body><div id="screen"></div></body></html>"""


def _install_novnc_stack(container_name):
    """Install Xvfb + x11vnc + noVNC + fluxbox on demand. Skips if already present."""
    check = subprocess.run(
        ["docker", "exec", container_name, "which", "Xvfb"],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode == 0:
        return  # Already installed
    subprocess.run(
        ["docker", "exec", "-u", "root", container_name, "bash", "-c",
         "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
         "apt-get install -y --no-install-recommends "
         "xvfb x11vnc novnc fluxbox x11-xserver-utils xterm python3-tk "
         "&& rm -rf /var/lib/apt/lists/* "
         # Create index.html redirect so iframe loads VNC viewer
         "&& echo '<html><head><meta http-equiv=\"refresh\" "
         "content=\"0;url=sandbox.html\">"
         "</head></html>' > /usr/share/novnc/index.html"],
        capture_output=True, text=True, timeout=300,
    )
    # Deploy custom fullscreen VNC viewer (no toolbar, fills entire viewport)
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as tf:
        tf.write(SANDBOX_VNC_HTML)
        tf_path = tf.name
    try:
        subprocess.run(
            ["docker", "cp", tf_path, f"{container_name}:/usr/share/novnc/sandbox.html"],
            capture_output=True, text=True, timeout=10,
        )
    finally:
        os.unlink(tf_path)


# Shell script written into the container as /usr/local/bin/novnc-wrap.
# Usage: novnc-wrap <port> <command...>
# Starts Xvfb → fluxbox → user app → x11vnc → noVNC (foreground).
NOVNC_WRAPPER = r"""#!/bin/bash
set -e
PORT="${1:?usage: novnc-wrap PORT command...}"
shift
DISPLAY=:99
export DISPLAY

# Start virtual framebuffer (1280x720, 24-bit color)
Xvfb $DISPLAY -screen 0 1024x768x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!
sleep 1

# Lightweight window manager
fluxbox &
sleep 1

# Configure fluxbox: hide toolbar, no decorations, maximize all windows
# (must write AFTER fluxbox creates defaults, then reconfigure)
cat > ~/.fluxbox/apps <<'FBAPPS'
[app] (name=.*)
  [Maximized] {yes}
  [Deco] {NONE}
[end]
FBAPPS
sed -i '/toolbar.visible/d' ~/.fluxbox/init
echo 'session.screen0.toolbar.visible: false' >> ~/.fluxbox/init
kill -USR2 $(pgrep -x fluxbox) 2>/dev/null || true
sleep 0.3

# Launch the user's GUI application (opens maximized, no decorations)
"$@" &
APP_PID=$!

# VNC server on display :99, listening on port 5900, no password
x11vnc -display $DISPLAY -forever -shared -nopw -rfbport 5900 -q &
sleep 0.5

# noVNC websocket proxy: serves HTML5 VNC client on $PORT
# websockify is bundled with the novnc package
NOVNC_PATH=$(find /usr -path "*/novnc/utils/novnc_proxy" -o -path "*/novnc/utils/launch.sh" 2>/dev/null | head -1)
if [ -z "$NOVNC_PATH" ]; then
    # Fallback: run websockify directly (noVNC static files still served)
    NOVNC_PATH=$(find /usr -name "websockify" -type f 2>/dev/null | head -1)
    if [ -n "$NOVNC_PATH" ]; then
        NOVNC_WEB=$(find /usr -path "*/novnc" -type d 2>/dev/null | head -1)
        exec $NOVNC_PATH --web="$NOVNC_WEB" $PORT localhost:5900
    else
        echo "ERROR: Cannot find noVNC or websockify" >&2
        exit 1
    fi
fi
exec $NOVNC_PATH --listen $PORT --vnc localhost:5900
"""


async def handle_sandbox_start(args):
    """Start a sandbox: ensure Docker container exists and run command inside it.

    Three display modes (auto-detected):
    - Web: command serves HTTP natively (flask, uvicorn, etc.) → direct
    - GUI: command needs X display (tkinter, pygame, Qt) → Xvfb + noVNC wrap
    - Terminal: everything else (TUIs, REPLs, scripts) → ttyd wrap
    All modes serve HTTP on the mapped port for the sandbox iframe.
    """
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container

    project_name = args["project"]
    command_override = args.get("command")
    port_override = args.get("port")

    log_query("sandbox_start", project_name, args)

    # Sweep stale sandbox entries: verify all "running" rows against Docker
    try:
        with db_connection() as conn:
            stale_rows = conn.execute(
                "SELECT id, container_name FROM alpha_builds WHERE status = 'running'"
            ).fetchall()
            for row in stale_rows:
                try:
                    check = subprocess.run(
                        ["docker", "inspect", "-f", "{{.State.Running}}", row["container_name"]],
                        capture_output=True, text=True, timeout=5,
                    )
                    alive = check.returncode == 0 and check.stdout.strip() == "true"
                except Exception:
                    alive = False
                if not alive:
                    conn.execute(
                        "UPDATE alpha_builds SET status = 'stopped' WHERE id = ?",
                        (row["id"],),
                    )
            conn.commit()
    except Exception:
        pass  # Don't block start on cleanup failure

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    # Determine command
    detected_app_type = None
    if command_override:
        command = command_override
        port = port_override
        # Smart default: if command looks like a web server and no port given, use 8080
        if not port and command:
            if _is_web_command(command):
                port = 8080
    else:
        command, port, detected_app_type = _detect_sandbox_command(project_path)
        if not command:
            return [TextContent(
                type="text",
                text=f"Could not auto-detect dev command for '{project_name}'. "
                     "Pass a 'command' argument (e.g., 'npm run dev').",
            )]

    # --- Display mode detection ---
    # Three modes: web (direct HTTP), gui (noVNC desktop), terminal (ttyd)
    # Rules:
    #   - If user explicitly passed a port → assume web server, don't wrap
    #   - If command matches web hints → web, don't wrap
    #   - If already wrapped (ttyd/novnc-wrap) → don't wrap (restart recovery)
    #   - If command matches GUI hints → noVNC desktop wrap
    #   - Otherwise → ttyd terminal wrap
    original_command = command
    is_ttyd_wrapped = command.startswith("ttyd ")
    is_novnc_wrapped = command.startswith("novnc-wrap ")
    user_gave_port = port_override is not None
    display_mode = "web"  # default

    is_web = user_gave_port or _is_web_command(command) or detected_app_type == "web"
    if is_web or is_ttyd_wrapped or is_novnc_wrapped:
        # Already handled or explicitly web — keep as-is
        if is_ttyd_wrapped:
            display_mode = "terminal"
        elif is_novnc_wrapped:
            display_mode = "gui"
    elif _is_gui_command(command) or detected_app_type == "gui":
        # Desktop GUI app — wrap with noVNC
        if not port:
            port = 8080
        display_mode = "gui"
        # Actual wrapping happens after container creation (need to install stack first)
    else:
        # Terminal app — wrap with tmux + ttyd
        # The app runs inside a tmux session ("sandbox"), and ttyd attaches to
        # that session.  This way multiple viewers (PC + laptop) share the SAME
        # terminal — no duplicate processes, no rendering glitches.
        # Output is also tee'd to /tmp/sandbox.log so sandbox_logs can read it.
        if not port:
            port = 8080
        import shlex
        # Build the tmux launch + ttyd attach command:
        #   1. Kill any stale tmux session
        #   2. Start tmux with the real command (+ tee to log)
        #   3. ttyd attaches to that tmux session (shared view)
        tmux_inner = f'stty cols 120 rows 40 2>/dev/null; {command} 2>&1 | tee -a /tmp/sandbox.log'
        tmux_setup = (
            f"tmux kill-session -t sandbox 2>/dev/null; "
            f"tmux new-session -d -s sandbox -x 120 -y 40 {shlex.quote(tmux_inner)}; "
            f"sleep 0.5"
        )
        # ttyd runs tmux attach — every browser tab shares one session
        command = (
            f"bash -c {shlex.quote(tmux_setup)} && "
            f"ttyd -p {port} -W -t fontSize=14 -t disableReconnect=true "
            f"tmux attach -t sandbox"
        )
        is_ttyd_wrapped = True
        display_mode = "terminal"

    try:
        # Get or create the Docker container (with port mapping if needed)
        container_name = _get_or_create_container(
            project_name, project_path, (project["stack"] or ""), port=port
        )

        # --- Auto-install dependencies if present ---
        _auto_install_deps(container_name, project_path)

        # --- noVNC setup for GUI apps ---
        if display_mode == "gui":
            _install_novnc_stack(container_name)
            # Always (re)write the wrapper script so code changes take effect
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
                tf.write(NOVNC_WRAPPER)
                tf_path = tf.name
            try:
                subprocess.run(
                    ["docker", "cp", tf_path, f"{container_name}:/usr/local/bin/novnc-wrap"],
                    capture_output=True, text=True, timeout=10,
                )
                subprocess.run(
                    ["docker", "exec", "-u", "root", container_name,
                     "chmod", "+x", "/usr/local/bin/novnc-wrap"],
                    capture_output=True, text=True, timeout=10,
                )
            finally:
                os.unlink(tf_path)
            if not is_novnc_wrapped:
                command = f"novnc-wrap {port} {command}"
                is_novnc_wrapped = True

        # Stop any existing log reader before starting a new one
        _log_reader_stop.set()

        _sandbox_log.clear()
        with _sandbox_state_lock:
            _sandbox_project = project_name
            _sandbox_command = command
            _sandbox_port = port
            _sandbox_container = container_name

        # Run the command inside the container (detached via docker exec + nohup)
        subprocess.run(
            ["docker", "exec", "-d", "-w", "/workspace", container_name,
             "bash", "-c",
             f"rm -f /tmp/sandbox.log; nohup {command} > /tmp/sandbox_wrapper.log 2>&1 &"],
            capture_output=True, text=True, timeout=10,
        )

        # Update alpha_builds with command, port info
        with db_connection() as conn2:
            ports_json = json.dumps({str(port): str(port)}) if port else "{}"
            conn2.execute(
                """UPDATE alpha_builds SET command=?, ports=?, status='running',
                   started_at=datetime('now') WHERE container_name=?""",
                (command, ports_json, container_name),
            )
            conn2.commit()

        # Start log reader with a fresh stop event
        _log_reader_stop.clear()
        threading.Thread(
            target=_docker_log_reader, args=(container_name, _log_reader_stop),
            daemon=True,
        ).start()

        port_info = f" on port {port}" if port else ""
        mode_note = ""
        if display_mode == "terminal":
            mode_note = " (via ttyd terminal)"
        elif display_mode == "gui":
            mode_note = " (via noVNC desktop)"
        return [TextContent(
            type="text",
            text=f"Started `{original_command}`{port_info}{mode_note} in container {container_name} for {project_name}.",
        )]

    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start sandbox: {e}")]


async def handle_sandbox_stop(args):
    """Stop the sandbox container."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container

    log_query("sandbox_stop")

    # Stop log reader thread
    _log_reader_stop.set()

    with _sandbox_state_lock:
        container = _sandbox_container
        name = _sandbox_project or "unknown"

    # If in-memory state lost (MCP restart), recover from DB
    if not container:
        with db_connection() as conn:
            row = conn.execute(
                """SELECT ab.container_name, p.name as project_name
                   FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                   WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
            ).fetchone()
        if row:
            container = row["container_name"]
            name = row["project_name"]
        else:
            return [TextContent(type="text", text="No sandbox is running.")]

    try:
        subprocess.run(
            ["docker", "stop", container],
            capture_output=True, text=True, timeout=30,
        )

        # Update alpha_builds DB
        with db_connection() as conn:
            conn.execute(
                "UPDATE alpha_builds SET status='stopped', stopped_at=datetime('now') WHERE container_name=?",
                (container,),
            )
            conn.commit()

        with _sandbox_state_lock:
            _sandbox_project = None
            _sandbox_command = None
            _sandbox_port = None
            _sandbox_container = None

        return [TextContent(type="text", text=f"Stopped sandbox for {name}.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error stopping sandbox: {e}")]


async def handle_sandbox_restart(args):
    """Restart the sandbox container."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container
    log_query("sandbox_restart")

    # Recover from DB if in-memory state lost (MCP restart)
    with _sandbox_state_lock:
        if not _sandbox_project or not _sandbox_command:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.container_name, ab.command, ab.ports, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                _sandbox_container = row["container_name"]
                _sandbox_project = row["project_name"]
                _sandbox_command = row["command"]
                ports = json.loads(row["ports"]) if row["ports"] else {}
                _sandbox_port = int(list(ports.keys())[0]) if ports else None
            else:
                return [TextContent(type="text", text="No sandbox to restart. Use sandbox_start first.")]

        project_name = _sandbox_project
        command = _sandbox_command
        port = _sandbox_port

    await handle_sandbox_stop({})
    start_args = {"project": project_name, "command": command}
    if port:
        start_args["port"] = port
    return await handle_sandbox_start(start_args)


async def handle_sandbox_status(args):
    """Get sandbox container status."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container
    log_query("sandbox_status")

    # Recover from DB if in-memory state lost (MCP restart)
    with _sandbox_state_lock:
        if not _sandbox_container:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.container_name, ab.command, ab.ports, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                _sandbox_container = row["container_name"]
                _sandbox_project = row["project_name"]
                _sandbox_command = row["command"]
                ports = json.loads(row["ports"]) if row["ports"] else {}
                _sandbox_port = int(list(ports.keys())[0]) if ports else None
            else:
                return [TextContent(type="text", text=json.dumps({
                    "status": "stopped",
                    "project": None,
                    "command": None,
                    "container": None,
                }))]

        container = _sandbox_container
        project = _sandbox_project
        command = _sandbox_command
        port = _sandbox_port

    # Check if container is running (outside the lock — slow operation)
    check = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    is_running = check.returncode == 0 and check.stdout.strip() == "true"

    with _sandbox_log_lock:
        error_count = sum(
            1 for line in _sandbox_log
            if "error" in line.lower() and "warning" not in line.lower()
        )
        warning_count = sum(1 for line in _sandbox_log if "warning" in line.lower())
        log_lines = len(_sandbox_log)

    # If container is dead, correct the DB and clear in-memory state
    if not is_running:
        with db_connection() as conn:
            conn.execute(
                "UPDATE alpha_builds SET status = 'stopped' WHERE container_name = ? AND status = 'running'",
                (container,),
            )
            conn.commit()
        with _sandbox_state_lock:
            _sandbox_project = None
            _sandbox_container = None
            _sandbox_command = None
            _sandbox_port = None

    result = {
        "status": "running" if is_running else "stopped",
        "project": project,
        "command": command,
        "container": container,
        "port": port,
        "log_lines": log_lines,
        "errors": error_count,
        "warnings": warning_count,
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_sandbox_logs(args):
    """Get logs from the sandbox container."""
    lines_count = args.get("lines", 50)
    log_filter = args.get("filter")

    with _sandbox_state_lock:
        sb_project = _sandbox_project
        sb_container = _sandbox_container

    log_query("sandbox_logs", sb_project, args)

    # Read from /tmp/sandbox.log inside the container (where nohup writes)
    if sb_container:
        try:
            result = subprocess.run(
                ["docker", "exec", sb_container,
                 "tail", "-n", str(lines_count), "/tmp/sandbox.log"],
                capture_output=True, text=True, timeout=10,
            )
            all_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            # Also grab docker logs as fallback
            if not all_lines or all_lines == [""]:
                result2 = subprocess.run(
                    ["docker", "logs", "--tail", str(lines_count), sb_container],
                    capture_output=True, text=True, timeout=10,
                )
                all_lines = (result2.stdout + result2.stderr).strip().split("\n")
        except Exception:
            with _sandbox_log_lock:
                all_lines = list(_sandbox_log)
    else:
        with _sandbox_log_lock:
            all_lines = list(_sandbox_log)

    if log_filter == "error":
        all_lines = [l for l in all_lines if "error" in l.lower()]
    elif log_filter == "warning":
        all_lines = [l for l in all_lines if "warning" in l.lower()]

    tail = all_lines[-lines_count:]

    result = {
        "project": sb_project,
        "container": sb_container,
        "total_lines": len(all_lines),
        "showing": len(tail),
        "filter": log_filter,
        "output": "\n".join(tail),
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_sandbox_test(args):
    """Run tests inside the sandbox container."""
    global _sandbox_project, _sandbox_container, _sandbox_command, _sandbox_port
    command_override = args.get("command")

    with _sandbox_state_lock:
        sb_project = _sandbox_project
        sb_container = _sandbox_container

    log_query("sandbox_test", sb_project, args)

    # Recover state from DB if globals lost (MCP restart)
    if not sb_container:
        try:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.*, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                with _sandbox_state_lock:
                    _sandbox_container = row["container_name"]
                    _sandbox_project = row["project_name"]
                    _sandbox_command = row["command"] or ""
                    ports = json.loads(row["ports"]) if row["ports"] else {}
                    _sandbox_port = int(list(ports.keys())[0]) if ports else None
                    sb_container = _sandbox_container
                    sb_project = _sandbox_project
        except Exception:
            pass

    if not sb_container:
        return [TextContent(
            type="text",
            text="No sandbox running. Use sandbox_start first.",
        )]

    if command_override:
        command = command_override
    else:
        # Auto-detect test command by checking files inside container
        project_path = None
        if sb_project:
            with db_connection() as conn:
                project = get_project_by_name(conn, sb_project)
            if project:
                project_path = _to_native_path(project["path"])

        if project_path:
            command = _detect_test_command(project_path)
        else:
            command = None

        if not command:
            return [TextContent(
                type="text",
                text="Could not auto-detect test command. Pass a 'command' argument.",
            )]

    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", sb_container,
             "bash", "-c", command],
            capture_output=True, text=True, timeout=120,
        )

        output = {
            "command": command,
            "container": sb_container,
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
    """Install dependencies inside the sandbox container."""
    project_name = args["project"]
    packages = args.get("packages", [])
    manager = args.get("manager")

    log_query("sandbox_install", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])

    # Ensure container exists
    container_name = _get_or_create_container(
        project_name, project_path, (project["stack"] or "")
    )

    # Auto-detect package manager
    if not manager:
        if os.path.isfile(os.path.join(project_path, "package.json")):
            manager = "npm"
        else:
            manager = "pip"

    try:
        if packages:
            # Sanitize package names to prevent shell injection
            import shlex
            safe_pkgs = " ".join(shlex.quote(p) for p in packages)
            if manager == "pip":
                install_cmd = f"pip install {safe_pkgs}"
            else:
                install_cmd = f"npm install {safe_pkgs}"
        else:
            if manager == "pip":
                install_cmd = "pip install -r requirements.txt"
            else:
                install_cmd = "npm install"

        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name,
             "bash", "-c", install_cmd],
            capture_output=True, text=True, timeout=180,
        )

        output = {
            "project": project_name,
            "container": container_name,
            "manager": manager,
            "packages": packages if packages else "from manifest",
            "exit_code": result.returncode,
            "success": result.returncode == 0,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Install timed out after 180 seconds.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to install: {e}")]


async def handle_sandbox_exec(args):
    """Run a command in the sandbox container and return output directly.

    Unlike sandbox_test (which requires a running sandbox), this just needs
    the Docker container to exist.  Perfect for diagnosing crashes, checking
    files, or running one-off commands.
    """
    project_name = args["project"]
    command = args["command"]
    timeout_s = min(args.get("timeout", 30), 120)

    log_query("sandbox_exec", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    container_name = f"alpha-{project_name}"

    # Ensure container is running
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        if "true" not in inspect.stdout.lower():
            subprocess.run(
                ["docker", "start", container_name],
                capture_output=True, text=True, timeout=15,
            )
    except Exception:
        # Container might not exist — try to create it
        _get_or_create_container(
            project_name, project_path, (project["stack"] or "")
        )

    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name,
             "bash", "-c", command],
            capture_output=True, text=True, timeout=timeout_s,
        )

        output = {
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text=f"Command timed out after {timeout_s}s.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to exec: {e}")]


# --- Penpot Helpers ---

PENPOT_BASE = os.environ.get("PENPOT_URL", "http://localhost:9001")
PENPOT_EMAIL = os.environ.get("PENPOT_EMAIL", "admin@local.dev")
PENPOT_PASSWORD = os.environ.get("PENPOT_PASSWORD", "admin123")

_penpot_session = None


def _penpot_rpc(command, payload=None, _retry=False):
    """Call a Penpot RPC command. Auto-authenticates on first call.

    Thread-safe: uses _penpot_lock to prevent concurrent session creation.
    """
    global _penpot_session

    try:
        import requests
    except ImportError:
        raise RuntimeError("requests library required for Penpot tools: pip install requests")

    with _penpot_lock:
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
            # Newer Penpot versions require Bearer token, not just session cookie
            token = resp.cookies.get("auth-token")
            if token:
                _penpot_session.headers["Authorization"] = f"Bearer {token}"
        session = _penpot_session

    resp = session.post(
        f"{PENPOT_BASE}/api/rpc/command/{command}",
        json=payload or {},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        if _retry:
            raise RuntimeError(f"Penpot RPC '{command}' failed ({resp.status_code}): {resp.text[:200]}")
        # Session may have expired — close old session, retry once
        with _penpot_lock:
            try:
                _penpot_session.close()
            except Exception:
                pass
            _penpot_session = None
        return _penpot_rpc(command, payload, _retry=True)

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


# --- Laptop Bridge (proxied via HTTP to remote MCP server) ---

LAPTOP_BRIDGE_URL = os.environ.get(
    "LAPTOP_BRIDGE_URL", "http://172.21.32.1:8222"
)
LAPTOP_BRIDGE_TOKEN = os.environ.get(
    "LAPTOP_BRIDGE_TOKEN", "ZEnTtzE_vd6WWeaInXKaBqjFIMIqlIhPXa1uhofedhk"
)


def _laptop_bridge_call(tool_name, arguments):
    """Call a tool on the laptop bridge MCP server via its SSE/HTTP endpoint."""
    import requests as _req

    # The laptop bridge is a standard HTTP API — we call tools via JSON-RPC over HTTP
    # But since it's an MCP SSE server, we use the simpler approach: call the tool
    # handlers directly via a lightweight HTTP wrapper we add to the bridge.
    # For now, use the /health endpoint pattern — post tool calls as JSON.
    url = f"{LAPTOP_BRIDGE_URL}/tool"
    headers = {"Content-Type": "application/json"}
    if LAPTOP_BRIDGE_TOKEN:
        headers["Authorization"] = f"Bearer {LAPTOP_BRIDGE_TOKEN}"

    try:
        resp = _req.post(
            url, json={"tool": tool_name, "arguments": arguments},
            headers=headers, timeout=130,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("result", data)
        else:
            return {"error": f"Bridge returned {resp.status_code}: {resp.text[:500]}"}
    except _req.ConnectionError:
        return {"error": "Cannot reach laptop bridge. Is the laptop on and connected via Tailscale?"}
    except _req.Timeout:
        return {"error": "Laptop bridge request timed out (130s)."}
    except Exception as e:
        return {"error": f"Laptop bridge error: {e}"}


async def handle_laptop_bridge(name, args):
    """Proxy a laptop_* tool call to the laptop bridge server."""
    log_query(name, None, args)
    result = _laptop_bridge_call(name, args)
    if isinstance(result, dict) and "error" in result:
        return [TextContent(type="text", text=result["error"])]
    elif isinstance(result, str):
        return [TextContent(type="text", text=result)]
    else:
        return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_laptop_download(args):
    """Download a binary file from the laptop to the PC."""
    import requests as _req

    remote_path = args.get("remote_path", "")
    local_path = args.get("local_path", "")

    if not remote_path or not local_path:
        return [TextContent(type="text", text="Both remote_path and local_path are required.")]

    log_query("laptop_download_file", None, args)

    url = f"{LAPTOP_BRIDGE_URL}/download"
    headers = {}
    if LAPTOP_BRIDGE_TOKEN:
        headers["Authorization"] = f"Bearer {LAPTOP_BRIDGE_TOKEN}"

    try:
        resp = _req.get(url, params={"path": remote_path}, headers=headers,
                        timeout=60, stream=True)
        if resp.status_code == 200:
            os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            size = os.path.getsize(local_path)
            return [TextContent(
                type="text",
                text=f"Downloaded {remote_path} -> {local_path} ({size:,} bytes)",
            )]
        else:
            return [TextContent(type="text", text=f"Download failed: {resp.status_code} {resp.text[:500]}")]
    except _req.ConnectionError:
        return [TextContent(type="text", text="Cannot reach laptop bridge. Is the laptop on and connected via Tailscale?")]
    except _req.Timeout:
        return [TextContent(type="text", text="Download timed out (60s).")]
    except Exception as e:
        return [TextContent(type="text", text=f"Download error: {e}")]


# --- Agent Factory Handlers ---


def _resolve_agent(conn, identifier):
    """Look up an agent by name or ID. Returns Row or None."""
    # Try by ID first
    try:
        aid = int(identifier)
        row = conn.execute("SELECT * FROM agents WHERE id = ? AND status = 'active'", (aid,)).fetchone()
        if row:
            return row
    except (ValueError, TypeError):
        pass
    # Try exact name
    row = conn.execute(
        "SELECT * FROM agents WHERE name = ? AND status = 'active'", (identifier,)
    ).fetchone()
    if row:
        return row
    # Case-insensitive
    row = conn.execute(
        "SELECT * FROM agents WHERE LOWER(name) = LOWER(?) AND status = 'active'", (identifier,)
    ).fetchone()
    return row


async def handle_agent_list(args):
    """List all agents."""
    status = args.get("status", "active")
    log_query("agent_list", None, args)

    with db_connection() as conn:
        rows = conn.execute(
            """SELECT a.*, p.name as project_name,
                      (SELECT COUNT(*) FROM agent_runs ar WHERE ar.agent_id = a.id) as run_count,
                      (SELECT MAX(ar.started_at) FROM agent_runs ar WHERE ar.agent_id = a.id) as last_run
               FROM agents a
               LEFT JOIN projects p ON p.id = a.project_id
               WHERE a.status = ?
               ORDER BY a.name""",
            (status,),
        ).fetchall()

    if not rows:
        return [TextContent(type="text", text=f"No agents with status '{status}'.")]

    lines = [f"Found {len(rows)} agent(s):\n"]
    for r in rows:
        project = r["project_name"] or "unbound"
        desc = (r["description"] or "")[:80]
        lines.append(
            f"  [{r['id']}] {r['name']} ({r['model']}) — project: {project}, "
            f"runs: {r['run_count']}, last: {(r['last_run'] or 'never')[:16]}"
        )
        if desc:
            lines.append(f"      {desc}")
    return [TextContent(type="text", text="\n".join(lines))]


async def handle_agent_create(args):
    """Create a new agent."""
    name = args.get("name", "").strip()
    system_prompt = args.get("system_prompt", "").strip()
    description = args.get("description", "").strip()
    model = args.get("model", "sonnet")
    project_name = args.get("project", "")
    max_turns = args.get("max_turns", 20)
    log_query("agent_create", project_name, args)

    if not name:
        return [TextContent(type="text", text="Error: 'name' is required.")]
    if not system_prompt:
        return [TextContent(type="text", text="Error: 'system_prompt' is required.")]
    if model not in ("sonnet", "opus", "haiku"):
        return [TextContent(type="text", text=f"Error: model must be 'sonnet', 'opus', or 'haiku', got '{model}'.")]

    with db_connection() as conn:
        # Check name uniqueness
        existing = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
        if existing:
            return [TextContent(type="text", text=f"Error: agent '{name}' already exists (ID {existing['id']}).")]

        # Resolve project if provided
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Error: project '{project_name}' not found.")]
            project_id = project["id"]

        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO agents (name, description, system_prompt, model,
               project_id, max_turns, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, description, system_prompt, model, project_id, max_turns, now),
        )
        conn.commit()
        agent_id = cursor.lastrowid

    return [TextContent(
        type="text",
        text=f"Agent '{name}' created (ID {agent_id}, model: {model}, max_turns: {max_turns}).",
    )]


async def handle_agent_update(args):
    """Update an existing agent."""
    identifier = args.get("agent", "")
    log_query("agent_update", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    with db_connection() as conn:
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]

        updates = []
        params = []

        if "name" in args and args["name"]:
            updates.append("name = ?")
            params.append(args["name"].strip())
        if "system_prompt" in args and args["system_prompt"]:
            updates.append("system_prompt = ?")
            params.append(args["system_prompt"].strip())
        if "description" in args:
            updates.append("description = ?")
            params.append(args["description"].strip())
        if "model" in args:
            if args["model"] not in ("sonnet", "opus", "haiku"):
                return [TextContent(type="text", text=f"Error: model must be 'sonnet', 'opus', or 'haiku'.")]
            updates.append("model = ?")
            params.append(args["model"])
        if "project" in args:
            if args["project"] == "":
                updates.append("project_id = ?")
                params.append(None)
            else:
                project = get_project_by_name(conn, args["project"])
                if not project:
                    return [TextContent(type="text", text=f"Project '{args['project']}' not found.")]
                updates.append("project_id = ?")
                params.append(project["id"])
        if "max_turns" in args:
            updates.append("max_turns = ?")
            params.append(int(args["max_turns"]))

        if not updates:
            return [TextContent(type="text", text="Nothing to update — pass at least one field to change.")]

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(agent["id"])

        conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    return [TextContent(
        type="text",
        text=f"Agent '{agent['name']}' (ID {agent['id']}) updated: {', '.join(u.split(' =')[0] for u in updates[:-1])}.",
    )]


async def handle_agent_delete(args):
    """Soft-delete an agent."""
    identifier = args.get("agent", "")
    log_query("agent_delete", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    with db_connection() as conn:
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]

        conn.execute("UPDATE agents SET status = 'deleted' WHERE id = ?", (agent["id"],))
        conn.commit()

    return [TextContent(type="text", text=f"Agent '{agent['name']}' (ID {agent['id']}) deleted.")]


async def handle_agent_run(args):
    """Run an agent via Claude CLI subprocess and return the result."""
    identifier = args.get("agent", "")
    user_prompt = args.get("prompt", "")
    log_query("agent_run", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    with db_connection() as conn:
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]
        agent = dict(agent)

        # Resolve project working directory
        cwd = os.path.dirname(os.path.abspath(__file__))  # fallback to custodian dir
        if agent.get("project_id"):
            proj = conn.execute(
                "SELECT path FROM projects WHERE id = ?", (agent["project_id"],)
            ).fetchone()
            if proj:
                cwd = _to_native_path(proj["path"])

        # Create run record
        cursor = conn.execute(
            """INSERT INTO agent_runs (agent_id, input, triggered_by, status)
               VALUES (?, ?, 'mcp', 'running')""",
            (agent["id"], user_prompt or agent.get("description", "")),
        )
        conn.commit()
        run_id = cursor.lastrowid

    # Build Claude CLI command
    env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)

    model_flag = {"sonnet": "sonnet", "opus": "opus", "haiku": "haiku"}.get(
        agent["model"], "sonnet"
    )

    cmd = [
        "claude", "-p", "--verbose",
        "--output-format", "stream-json",
        "--model", model_flag,
        "--max-turns", str(agent["max_turns"] or 20),
        "--append-system-prompt", agent["system_prompt"],
    ]

    prompt_text = user_prompt or agent.get("description") or f"Execute your purpose as {agent['name']}"

    output_parts = []
    tokens_used = 0
    cost_usd = None

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
        )
        proc.stdin.write(prompt_text)
        proc.stdin.close()

        for raw_line in iter(proc.stdout.readline, ""):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
                # Extract text from stream events
                t = event.get("type", "")
                subtype = event.get("subtype", "")

                if t == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        output_parts.append(delta.get("text", ""))
                elif t == "assistant" and subtype == "text":
                    output_parts.append(event.get("text", ""))
                elif t == "assistant" and "message" in event:
                    for block in event["message"].get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            output_parts.append(block["text"])

                if t == "result":
                    tokens_used = event.get("usage", {}).get("total_tokens", 0)
                    cost_usd = event.get("cost_usd")
            except json.JSONDecodeError:
                continue

        proc.wait(timeout=600)

        full_output = "".join(output_parts)

        if proc.returncode != 0:
            stderr = proc.stderr.read().strip()
            with db_connection() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='failed', output=?, error=?,
                       tokens_used=?, finished_at=datetime('now') WHERE id=?""",
                    (full_output, stderr, tokens_used, run_id),
                )
                conn.commit()
            return [TextContent(
                type="text",
                text=f"Agent '{agent['name']}' failed (run #{run_id}).\nError: {stderr}\nOutput: {full_output[:2000]}",
            )]

        # Success
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='completed', output=?,
                   tokens_used=?, finished_at=datetime('now') WHERE id=?""",
                (full_output, tokens_used, run_id),
            )
            conn.commit()

        meta = [f"run #{run_id}"]
        if tokens_used:
            meta.append(f"{tokens_used} tokens")
        if cost_usd is not None:
            meta.append(f"${cost_usd:.4f}")

        return [TextContent(
            type="text",
            text=f"Agent '{agent['name']}' completed ({', '.join(meta)}).\n\n{full_output}",
        )]

    except FileNotFoundError:
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', error='Claude CLI not found',
                   finished_at=datetime('now') WHERE id=?""",
                (run_id,),
            )
            conn.commit()
        return [TextContent(type="text", text="Error: 'claude' CLI not found on PATH.")]
    except subprocess.TimeoutExpired:
        proc.kill()
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', error='Timed out (600s)',
                   finished_at=datetime('now') WHERE id=?""",
                (run_id,),
            )
            conn.commit()
        return [TextContent(type="text", text=f"Agent '{agent['name']}' timed out after 600s.")]
    except Exception as e:
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', error=?,
                   finished_at=datetime('now') WHERE id=?""",
                (str(e), run_id),
            )
            conn.commit()
        return [TextContent(type="text", text=f"Agent run error: {e}")]


async def handle_agent_runs(args):
    """Get run history for agents."""
    identifier = args.get("agent", "")
    limit = min(args.get("limit", 10), 50)
    log_query("agent_runs", None, args)

    with db_connection() as conn:
        agent_id = None
        agent_name = "all agents"
        if identifier:
            agent = _resolve_agent(conn, identifier)
            if not agent:
                return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]
            agent_id = agent["id"]
            agent_name = agent["name"]

        if agent_id:
            rows = conn.execute(
                """SELECT ar.*, a.name as agent_name
                   FROM agent_runs ar
                   JOIN agents a ON a.id = ar.agent_id
                   WHERE ar.agent_id = ?
                   ORDER BY ar.started_at DESC LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ar.*, a.name as agent_name
                   FROM agent_runs ar
                   JOIN agents a ON a.id = ar.agent_id
                   ORDER BY ar.started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    if not rows:
        return [TextContent(type="text", text=f"No runs found for {agent_name}.")]

    lines = [f"Last {len(rows)} run(s) for {agent_name}:\n"]
    for r in rows:
        status_icon = {"completed": "+", "failed": "X", "running": "~"}.get(r["status"], "?")
        tokens = r["tokens_used"] or 0
        output_preview = (r["output"] or "")[:100].replace("\n", " ")
        lines.append(
            f"  [{status_icon}] #{r['id']} {r['agent_name']} — {r['status']} "
            f"({(r['started_at'] or '')[:16]}, {tokens} tokens)"
        )
        if r["error"]:
            lines.append(f"      Error: {r['error'][:100]}")
        elif output_preview:
            lines.append(f"      {output_preview}...")
    return [TextContent(type="text", text="\n".join(lines))]


def _cleanup():
    """Clean up background resources on shutdown."""
    global _penpot_session

    print("[custodian] Cleaning up...", file=sys.stderr)

    # Stop docker log reader
    _log_reader_stop.set()

    # Kill spawned subprocesses
    with _background_procs_lock:
        for proc in _background_procs:
            try:
                proc.kill()
            except OSError:
                pass
        _background_procs.clear()

    # Close Penpot session
    with _penpot_lock:
        if _penpot_session is not None:
            try:
                _penpot_session.close()
            except Exception:
                pass
            _penpot_session = None


async def main():
    import asyncio as _aio

    loop = _aio.get_event_loop()

    def _shutdown(sig, frame):
        print(f"[custodian] Received signal {sig}, shutting down...", file=sys.stderr)
        _cleanup()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        _cleanup()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
