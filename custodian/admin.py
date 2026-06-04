#!/usr/bin/env python3
"""NAI Workbench — ADMIN 01: Custodian Administration TUI.

10 tabs:
- Projects: Import from GitHub, register local, manage projects
- Custodian: Index projects (trigger Sonnet)
- Fossils: Browse fossil history, view details, compare
- Detective: Run analysis, view insights, refine prompts
- Status: System overview (DB stats, project status)
- Editor: File browser + code editor + persistent OpenCode chat
- Agent Factory: Create, configure, and run AI agents (Claude Agent SDK)
- Alpha Builds: Docker container-based project sandboxes
- Devices: Multi-device pairing and management
- Ticker: Configure scrolling ticker overlay segments and settings
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Log,
    OptionList,
    RichLog,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)
from textual.binding import Binding
from textual import work

from opencode_runner import OpenCodeRunnerError, list_available_models, run_opencode

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
PROJECTS_DIR = os.path.expanduser("~/projects")
OPENCODE_BIN = os.environ.get("NAI_WORKBENCH_OPENCODE_BIN", os.path.expanduser("~/.opencode/bin/opencode"))
OPENCODE_MODEL = os.environ.get("NAI_WORKBENCH_OPENCODE_MODEL", "openai/gpt-5.4")
BOX_TOOL_PORT_MIN = 9100
BOX_TOOL_PORT_MAX = 9199
BOX_TOOL_SERVER_CONTAINER_PATH = "/opt/box-tools/server.py"

# Detect WSL and provide path translation
import platform as _platform
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


def _load_agent_model_choices():
    """Load available OpenCode model IDs once for the Agent Factory form."""
    try:
        models = list_available_models()
    except OpenCodeRunnerError as e:
        return [], None, str(e)
    if not models:
        return [], None, "OpenCode returned no available OpenAI models."
    default = "openai/gpt-5.4" if "openai/gpt-5.4" in models else models[0]
    return [(model, model) for model in models], default, None


def _agent_model_options_with_current(options, current_model):
    """Include the saved model even if it no longer appears in the live list."""
    if not current_model:
        return options

    values = {value for _, value in options}
    if current_model in values:
        return options

    return options + [(f"{current_model} (deprecated)", current_model)]

# Startup prompt injected into every new Editor OpenCode session so it knows about
# the fossil system, MCP tools, and registered projects — same architecture
# as the rest of the custodian system.
EDITOR_SYSTEM_PROMPT = """\
You are an on-demand developer embedded in the NAI Workbench Admin TUI. \
When the user asks you to do something, DO IT — edit files, write code, run \
commands, fix bugs, add features. You are not a chatbot. You are a developer.

WORKFLOW:
1. Use get_project_fossil(name) FIRST to get architecture context before touching any project
2. Use lookup_symbol(name, project) to find functions/classes with live line numbers
3. Read the actual file to see current code
4. Make your changes with Edit or Write
5. Run tests or commands with Bash to verify your work
6. The user's editor auto-reloads when you modify the open file

CUSTODIAN MCP TOOLS (query the fossil database — faster than searching):
- list_projects() — registered projects with status
- get_project_fossil(project_name) — architecture, file tree, dependencies, summary
- lookup_symbol(name, project_name) — live tree-sitter search, always-current line numbers
- get_symbol_context(name, project_name) — Sonnet's descriptions + relationship graph
- find_related_files(symbol_name, project_name) — files to touch for a change
- get_recent_changes(project_name) — summarized recent commits
- get_detective_insights(project_name) — coupling patterns, architectural warnings
- trigger_custodian(project_name) — re-index a project with Sonnet

STANDARD TOOLS: Read, Edit, Write, Bash, Glob, Grep — all fully available.

RULES:
- Make real code changes. Do not just describe what to do.
- If a file is shown in <file> tags, that is the file currently open in the editor.
- After editing, always verify your changes compile/work.
- Keep changes minimal and focused — don't refactor things you weren't asked to touch.
- If you need more context, use the fossil/symbol tools before grepping blindly.
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_projects():
    """Get all active projects."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM projects WHERE status = 'active' ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_projects():
    """Get all projects including inactive."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_fossils(project_id=None):
    """Get fossil history, optionally filtered by project."""
    conn = get_db()
    if project_id:
        rows = conn.execute(
            """SELECT f.*, p.name as project_name
               FROM fossils f JOIN projects p ON p.id = f.project_id
               WHERE f.project_id = ?
               ORDER BY f.created_at DESC""",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT f.*, p.name as project_name
               FROM fossils f JOIN projects p ON p.id = f.project_id
               ORDER BY f.created_at DESC""",
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_insights(project_id=None, limit=20):
    """Get detective insights."""
    conn = get_db()
    if project_id:
        rows = conn.execute(
            """SELECT di.*, p.name as project_name
               FROM detective_insights di
               LEFT JOIN projects p ON p.id = di.project_id
               WHERE di.project_id = ? OR di.project_id IS NULL
               ORDER BY di.created_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT di.*, p.name as project_name
               FROM detective_insights di
               LEFT JOIN projects p ON p.id = di.project_id
               ORDER BY di.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_db_stats():
    """Get database statistics."""
    conn = get_db()
    stats = {
        "projects": conn.execute("SELECT COUNT(*) FROM projects WHERE status='active'").fetchone()[0],
        "fossils": conn.execute("SELECT COUNT(*) FROM fossils").fetchone()[0],
        "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "insights": conn.execute("SELECT COUNT(*) FROM detective_insights").fetchone()[0],
        "queries": conn.execute("SELECT COUNT(*) FROM query_log").fetchone()[0],
        "prompts": conn.execute("SELECT COUNT(*) FROM custodian_prompts").fetchone()[0],
        "db_size": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
    }
    conn.close()
    return stats


# ── Agent Factory CRUD ────────────────────────────────────────────────


def get_agents(status="active"):
    """Get all agents, optionally filtered by status."""
    conn = get_db()
    rows = conn.execute(
        """SELECT a.*, p.name as project_name
           FROM agents a
           LEFT JOIN projects p ON p.id = a.project_id
           WHERE a.status = ?
           ORDER BY a.name""",
        (status,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agent(agent_id):
    """Get a single agent by ID."""
    conn = get_db()
    row = conn.execute(
        """SELECT a.*, p.name as project_name
           FROM agents a
           LEFT JOIN projects p ON p.id = a.project_id
           WHERE a.id = ?""",
        (agent_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_agent(name, system_prompt, description="", model="openai/gpt-5.4",
               project_id=None, max_turns=20, tools=None, mcp_servers=None,
               agent_id=None):
    """Create or update an agent."""
    conn = get_db()
    tools_json = json.dumps(tools) if tools else None
    mcp_json = json.dumps(mcp_servers) if mcp_servers else None
    now = datetime.now().isoformat()

    if agent_id:
        conn.execute(
            """UPDATE agents SET name=?, description=?, system_prompt=?, model=?,
               project_id=?, max_turns=?, tools=?, mcp_servers=?, updated_at=?
               WHERE id=?""",
            (name, description, system_prompt, model, project_id, max_turns,
             tools_json, mcp_json, now, agent_id),
        )
    else:
        conn.execute(
            """INSERT INTO agents (name, description, system_prompt, model,
               project_id, max_turns, tools, mcp_servers, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, description, system_prompt, model, project_id, max_turns,
             tools_json, mcp_json, now),
        )
    conn.commit()
    conn.close()


def delete_agent(agent_id):
    """Soft-delete an agent."""
    conn = get_db()
    conn.execute("UPDATE agents SET status = 'deleted' WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()


def get_pipelines(status="active"):
    """Get all pipelines."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pipelines WHERE status = ? ORDER BY name",
        (status,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_pipeline(name, steps, description="", schedule=None, pipeline_id=None):
    """Create or update a pipeline."""
    conn = get_db()
    steps_json = json.dumps(steps) if isinstance(steps, (list, dict)) else steps

    if pipeline_id:
        conn.execute(
            "UPDATE pipelines SET name=?, description=?, steps=?, schedule=? WHERE id=?",
            (name, description, steps_json, schedule, pipeline_id),
        )
    else:
        conn.execute(
            "INSERT INTO pipelines (name, description, steps, schedule) VALUES (?, ?, ?, ?)",
            (name, description, steps_json, schedule),
        )
    conn.commit()
    conn.close()


def delete_pipeline(pipeline_id):
    """Soft-delete a pipeline."""
    conn = get_db()
    conn.execute("UPDATE pipelines SET status = 'deleted' WHERE id = ?", (pipeline_id,))
    conn.commit()
    conn.close()


def create_agent_run(agent_id, input_text="", triggered_by="manual",
                     pipeline_id=None, pipeline_step=None):
    """Create a new agent run record. Returns the run ID."""
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO agent_runs (agent_id, pipeline_id, pipeline_step, input,
           triggered_by, status) VALUES (?, ?, ?, ?, ?, 'running')""",
        (agent_id, pipeline_id, pipeline_step, input_text, triggered_by),
    )
    run_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return run_id


def complete_agent_run(run_id, status="completed", output="",
                       tokens_used=0, error=None):
    """Mark an agent run as complete."""
    conn = get_db()
    conn.execute(
        """UPDATE agent_runs SET status=?, output=?, tokens_used=?,
           error=?, finished_at=datetime('now') WHERE id=?""",
        (status, output, tokens_used, error, run_id),
    )
    conn.commit()
    conn.close()


def get_agent_runs(agent_id=None, limit=20):
    """Get recent agent runs."""
    conn = get_db()
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
    conn.close()
    return [dict(r) for r in rows]


def get_pending_reindex_requests():
    """Get all pending reindex requests."""
    conn = get_db()
    rows = conn.execute(
        """SELECT rr.*, p.name as project_name
           FROM reindex_requests rr
           JOIN projects p ON p.id = rr.project_id
           WHERE rr.status = 'pending'
           ORDER BY rr.requested_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_reindex(request_id):
    """Approve a reindex request and trigger indexing."""
    conn = get_db()
    req = conn.execute(
        """SELECT rr.*, p.name as project_name, p.path as project_path
           FROM reindex_requests rr
           JOIN projects p ON p.id = rr.project_id
           WHERE rr.id = ?""",
        (request_id,),
    ).fetchone()
    if not req:
        conn.close()
        return None

    conn.execute(
        "UPDATE reindex_requests SET status='approved', resolved_at=datetime('now') WHERE id=?",
        (request_id,),
    )
    conn.commit()
    conn.close()
    return dict(req)


def deny_reindex(request_id):
    """Deny a reindex request."""
    conn = get_db()
    conn.execute(
        "UPDATE reindex_requests SET status='denied', resolved_at=datetime('now') WHERE id=?",
        (request_id,),
    )
    conn.commit()
    conn.close()


# ── Alpha Builds CRUD ────────────────────────────────────────────────


def get_alpha_builds(project_id=None):
    """Get all alpha builds, optionally filtered by project."""
    conn = get_db()
    if project_id:
        rows = conn.execute(
            """SELECT ab.*, p.name as project_name
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.project_id = ?
               ORDER BY ab.id DESC""",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT ab.*, p.name as project_name
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               ORDER BY ab.id DESC"""
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_alpha_build(project_id, container_id=None, container_name=None,
                     image=None, status="stopped", ports=None, command=None,
                     build_log=None, build_id=None):
    """Create or update an alpha build."""
    conn = get_db()
    ports_json = json.dumps(ports) if ports else None

    if build_id:
        conn.execute(
            """UPDATE alpha_builds SET container_id=?, container_name=?, image=?,
               status=?, ports=?, command=?, build_log=? WHERE id=?""",
            (container_id, container_name, image, status, ports_json, command,
             build_log, build_id),
        )
    else:
        conn.execute(
            """INSERT INTO alpha_builds (project_id, container_id, container_name,
               image, status, ports, command, build_log, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (project_id, container_id, container_name, image, status,
             ports_json, command, build_log),
        )
    conn.commit()
    conn.close()


def register_project(name, path, stack=""):
    """Register a project in the custodian database."""
    conn = get_db()
    conn.execute(
        """INSERT INTO projects (name, path, stack)
           VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               path = excluded.path,
               stack = excluded.stack,
               status = 'active'""",
        (name, path, stack),
    )
    project = conn.execute(
        "SELECT id, name, path, stack FROM projects WHERE name = ?",
        (name,),
    ).fetchone()
    conn.commit()
    conn.close()

    try:
        _provision_project_box(project["id"], project["name"], project["path"], project["stack"] or "")
    except Exception as e:
        print(f"[project-box] Admin registration provision warning for {name}: {e}", file=sys.stderr)


def _pick_box_image(project_name, project_path, stack=""):
    """Match MCP server image selection without importing its runtime."""
    devcontainer_path = os.path.join(project_path, ".devcontainer")
    if os.path.isdir(devcontainer_path):
        dockerfile = os.path.join(devcontainer_path, "Dockerfile")
        if os.path.isfile(dockerfile):
            image_name = f"alpha-{project_name}:latest"
            subprocess.run(
                ["docker", "build", "-t", image_name, "-f", dockerfile, project_path],
                capture_output=True,
                text=True,
                timeout=300,
            )
            return image_name

    check = subprocess.run(
        ["docker", "image", "inspect", "nai-sandbox:latest"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check.returncode == 0:
        return "nai-sandbox:latest"
    if "Python" in stack:
        return "python:3.12"
    if any(s in stack for s in ("Node", "React", "Next", "Electron")):
        return "node:22"
    return "python:3.12"


def _auto_install_box_deps(container_name, project_path):
    """Best-effort dependency install for newly provisioned project boxes."""
    req_txt = os.path.join(project_path, "requirements.txt")
    pkg_json = os.path.join(project_path, "package.json")
    if os.path.isfile(req_txt):
        subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name, "bash", "-c", "pip install -q -r requirements.txt 2>/dev/null"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    elif os.path.isfile(pkg_json):
        subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name, "bash", "-c", "npm install --silent 2>/dev/null"],
            capture_output=True,
            text=True,
            timeout=120,
        )


def _allocate_tool_server_port(conn, project_id):
    existing = conn.execute(
        "SELECT tool_server_port FROM project_boxes WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if existing and existing[0]:
        return int(existing[0])

    used = {
        int(row[0])
        for row in conn.execute(
            "SELECT tool_server_port FROM project_boxes WHERE tool_server_port IS NOT NULL"
        ).fetchall()
    }
    for port in range(BOX_TOOL_PORT_MIN, BOX_TOOL_PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("No available tool server ports in range 9100-9199")


def _copy_and_start_box_tool_server(container_name, port):
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "box_tool_server.py")
    subprocess.run(
        ["docker", "exec", container_name, "mkdir", "-p", "/opt/box-tools"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    subprocess.run(
        ["docker", "cp", source_path, f"{container_name}:{BOX_TOOL_SERVER_CONTAINER_PATH}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    has_tools = subprocess.run(
        ["docker", "exec", container_name, "test", "-d", "/workspace/tools"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if has_tools.returncode != 0:
        return

    subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", f"pkill -f '{BOX_TOOL_SERVER_CONTAINER_PATH}' >/dev/null 2>&1 || true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        [
            "docker", "exec", "-d",
            "-e", f"BOX_TOOL_PORT={int(port)}",
            container_name,
            "python3", BOX_TOOL_SERVER_CONTAINER_PATH,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


def _provision_project_box(project_id, project_name, project_path, stack="", env_vars=None):
    """Best-effort local project box provisioning for admin-side registration."""
    env_vars = env_vars or {}
    native_project_path = _to_native_path(project_path)
    container_name = f"alpha-{project_name}"
    conn = get_db()
    tool_server_port = _allocate_tool_server_port(conn, project_id)
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}|{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if inspect.returncode == 0:
        image_name, running = (inspect.stdout.strip().split("|", 1) + [""])[:2]
        if running.strip().lower() != "true":
            start = subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if start.returncode != 0:
                raise RuntimeError((start.stderr or start.stdout or "docker start failed").strip())
    else:
        image_name = _pick_box_image(project_name, native_project_path, stack)
        run_cmd = [
            "docker", "run", "-d", "--network", "host", "--name", container_name,
            "-v", f"{native_project_path}:/workspace", "-w", "/workspace",
            "--restart", "unless-stopped",
        ]
        for key, value in sorted(env_vars.items()):
            run_cmd += ["-e", f"{key}={value}"]
        run_cmd += [image_name, "sleep", "infinity"]
        run = subprocess.run(run_cmd, capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            raise RuntimeError((run.stderr or run.stdout or "docker run failed").strip())
        _auto_install_box_deps(container_name, native_project_path)

    _copy_and_start_box_tool_server(container_name, tool_server_port)

    conn.execute(
        """
        INSERT INTO project_boxes (
            project_id, container_name, image, status, env_vars, ports, tool_server_port,
            restart_policy, error_message, created_at, updated_at
        )
        VALUES (?, ?, ?, 'running', ?, '{}', ?, 'unless-stopped', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(project_id) DO UPDATE SET
            container_name = excluded.container_name,
            image = excluded.image,
            status = 'running',
            env_vars = excluded.env_vars,
            ports = excluded.ports,
            tool_server_port = excluded.tool_server_port,
            restart_policy = excluded.restart_policy,
            error_message = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (project_id, container_name, image_name, json.dumps(env_vars), tool_server_port),
    )
    conn.commit()
    conn.close()


def detect_stack(project_path):
    """Auto-detect project stack from files."""
    parts = []
    checks = {
        "package.json": None,
        "requirements.txt": "Python",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pyproject.toml": "Python",
        "setup.py": "Python",
    }
    for filename, label in checks.items():
        if os.path.exists(os.path.join(project_path, filename)):
            if filename == "package.json":
                try:
                    with open(os.path.join(project_path, filename)) as f:
                        pkg = json.load(f)
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    if "next" in deps:
                        parts.append("Next.js")
                    elif "react" in deps:
                        parts.append("React")
                    elif "vue" in deps:
                        parts.append("Vue")
                    elif "svelte" in deps:
                        parts.append("Svelte")
                    if "electron" in deps:
                        parts.append("Electron")
                    if "typescript" in deps:
                        parts.append("TypeScript")
                    if not parts:
                        parts.append("Node.js")
                except (json.JSONDecodeError, OSError):
                    parts.append("Node.js")
            else:
                parts.append(label)
    return " + ".join(parts) if parts else ""


def slugify(name):
    """Convert a repo name to a project slug."""
    name = name.lower().strip()
    name = re.sub(r'[^a-z0-9]+', '-', name)
    return name.strip('-')


class WorkbenchDirectoryTree(DirectoryTree):
    """DirectoryTree that filters out junk files and directories."""

    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", ".next",
        "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".tox", "egg-info",
    }
    SKIP_EXTENSIONS = {
        ".db", ".db-wal", ".db-shm", ".pyc", ".pyo",
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
        ".woff", ".woff2", ".ttf", ".eot", ".lock",
    }

    def filter_paths(self, paths):
        results = []
        for p in paths:
            name = p.name
            if p.is_dir() and name in self.SKIP_DIRS:
                continue
            if p.suffix.lower() in self.SKIP_EXTENSIONS:
                continue
            results.append(p)
        return results


# ── CSS ───────────────────────────────────────────────────────────────

CSS = """
Screen {
    background: $surface;
}

#title-bar {
    dock: top;
    height: 3;
    background: $primary;
    color: $text;
    text-align: center;
    padding: 1;
    text-style: bold;
}

.tab-content {
    padding: 1 2;
}

.section-header {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}

.status-value {
    color: $text;
}

#custodian-log {
    height: 1fr;
    border: solid $primary;
    margin-top: 1;
}

#project-log {
    height: 12;
    border: solid $primary;
    margin-top: 1;
}

.action-bar {
    height: 3;
    margin-bottom: 1;
}

#ticker-config-bar {
    height: 3;
    margin-bottom: 1;
}

#ticker-config-bar Switch {
    width: auto;
    margin-right: 0;
}

#ticker-config-bar Label {
    margin-right: 2;
    content-align: center middle;
}

.input-bar {
    height: 3;
    margin-bottom: 1;
}

.project-selector {
    width: 40;
}

.big-button {
    width: 20;
    margin-left: 2;
}

.url-input {
    width: 1fr;
}

.name-input {
    width: 30;
}

.stack-input {
    width: 30;
}

#fossil-detail {
    height: 1fr;
    border: solid $primary;
    margin-top: 1;
}

#insight-detail {
    height: 1fr;
    border: solid $primary;
    margin-top: 1;
}

DataTable {
    height: auto;
    max-height: 15;
}

#gh-repos-table {
    height: auto;
    max-height: 12;
}

#editor-panel {
    height: 2fr;
}

#editor-tree {
    width: 30;
    border-right: solid $primary;
}

#editor-right {
    width: 1fr;
}

#editor-toolbar {
    height: 3;
    padding: 0 1;
}

#editor-file-label {
    width: 1fr;
    padding: 1;
}

#editor-textarea {
    height: 1fr;
    border: solid $primary;
}

#git-toolbar {
    height: 3;
    padding: 0 1;
    border-top: solid $accent;
    border-bottom: solid $accent;
}

#git-status-label {
    width: 1fr;
    padding: 1;
    color: $text-muted;
}

#chat-panel {
    height: 1fr;
    min-height: 12;
}

#chat-toolbar {
    height: 3;
    padding: 0 1;
}

#session-label {
    width: 1fr;
    padding: 1;
    color: $text-muted;
}

#claude-chat-log {
    height: 1fr;
    border: solid $primary;
}

#chat-input-bar {
    height: 3;
}

#chat-input {
    width: 1fr;
}

#clone-path-label {
    color: $text-muted;
    padding: 0 0 1 0;
}

#projects-split {
    height: 30;
}

#cards-panel {
    width: 35;
    overflow-y: auto;
}

#hierarchy-panel {
    width: 1fr;
    border-left: solid $primary;
}

#hierarchy-tree {
    height: 1fr;
}

#hierarchy-placeholder {
    color: $text-muted;
    padding: 1;
}

.project-card {
    margin: 0 1 1 0;
    padding: 1;
    border: solid $primary;
    height: auto;
    min-width: 30;
    text-align: left;
}

.project-card.indexed {
    border: solid green;
}

.project-card.not-indexed {
    border: solid gray;
}

.project-card.selected {
    border: double $accent;
    background: $surface-lighten-1;
}

#agent-editor-form {
    height: auto;
    padding: 1;
}

#agent-prompt-textarea {
    height: 8;
    border: solid $primary;
}

#agent-run-log {
    height: 1fr;
    border: solid $primary;
    margin-top: 1;
}

#agents-table {
    height: auto;
    max-height: 10;
}

#pipelines-table {
    height: auto;
    max-height: 8;
}

#runs-table {
    height: auto;
    max-height: 8;
}

#reindex-table {
    height: auto;
    max-height: 6;
}

#agents-left-panel {
    width: 40;
    border-right: solid $primary;
}

#agents-right-panel {
    width: 1fr;
}

#builds-left-panel {
    width: 35;
    border-right: solid $primary;
    overflow-y: auto;
}

#builds-right-panel {
    width: 1fr;
}

#builds-log {
    height: 1fr;
    border: solid $primary;
    margin-top: 1;
}

#build-info-label {
    padding: 1;
    color: $text;
}

.build-card {
    margin: 0 1 1 0;
    padding: 1;
    border: solid $primary;
    height: auto;
    min-width: 30;
    text-align: left;
}

.build-card.running {
    border: solid green;
}

.build-card.stopped {
    border: solid gray;
}

.build-card.building {
    border: solid yellow;
}

.build-card.failed {
    border: solid red;
}
"""


class CustodianAdmin(App):
    """NAI Workbench Admin TUI."""

    TITLE = "NAI WORKBENCH — ADMIN 01"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "focus_tab('projects')", "Projects"),
        Binding("i", "focus_tab('custodian')", "Custodian"),
        Binding("f", "focus_tab('fossils')", "Fossils"),
        Binding("d", "focus_tab('detective')", "Detective"),
        Binding("s", "focus_tab('status')", "Status"),
        Binding("e", "focus_tab('editor')", "Editor"),
        Binding("a", "focus_tab('agents')", "Agents"),
        Binding("b", "focus_tab('builds')", "Builds"),
        Binding("v", "focus_tab('devices')", "Devices"),
        Binding("t", "focus_tab('ticker')", "Ticker"),
    ]

    def __init__(self):
        super().__init__()
        self._projects = []
        self._selected_project_id = None
        self._selected_card_project_id = None
        self._card_gen = 0
        self._gh_repos = []
        # Editor tab state
        self._editor_current_file: str | None = None
        self._editor_modified: bool = False
        self._editor_project_name: str | None = None
        self._editor_project_path: str | None = None
        self._claude_session_id: str | None = None
        self._claude_process: subprocess.Popen | None = None
        self._claude_running: bool = False
        self._claude_edited_files: set = set()
        self._workbench_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._session_file = os.path.join(os.path.expanduser("~"), ".custodian_opencode_session")
        # Agent Factory state
        self._selected_agent_id: int | None = None
        self._agent_running: bool = False
        self._agent_model_options, self._agent_model_default, self._agent_model_error = _load_agent_model_choices()
        # Alpha Builds state
        self._selected_build_id: int | None = None
        self._build_card_gen = 0
        # Devices tab state
        self._selected_device_id: int | None = None
        # Ticker overlay state
        self._ticker_overlay_proc: subprocess.Popen | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("NAI WORKBENCH — ADMIN 01", id="title-bar")

        with TabbedContent("Projects", "Custodian", "Fossils", "Detective", "Status", "Editor", "Agent Factory", "Alpha Builds", "Devices", "Ticker"):
            # ── Projects Tab ─────────────────────────────────
            with TabPane("Projects", id="tab-projects"):
                with VerticalScroll(classes="tab-content"):
                    yield Static("Your Projects", classes="section-header")
                    with Horizontal(id="projects-split"):
                        # Left: scrollable project cards
                        with VerticalScroll(id="cards-panel"):
                            pass  # Cards populated dynamically in _refresh_project_cards()
                        # Right: hierarchy tree
                        with Vertical(id="hierarchy-panel"):
                            yield Static("Click a project to view its map", id="hierarchy-placeholder")
                            yield Tree("Projects", id="hierarchy-tree")

                    with Horizontal(classes="action-bar"):
                        yield Button("Remove Selected", variant="error", id="btn-remove-project")
                        yield Button("Reactivate", variant="default", id="btn-reactivate-project")
                        yield Button("Open in Editor", variant="primary", id="btn-card-to-editor")

                    yield Static("Quick Import", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Input(
                            placeholder="Paste GitHub URL or owner/repo...",
                            id="quick-import-input",
                        )
                        yield Button("Import", variant="success", id="btn-quick-import")

                    yield Static("Import from GitHub", classes="section-header")
                    yield Static(
                        f"Projects clone to: {PROJECTS_DIR}/",
                        id="clone-path-label",
                    )
                    with Horizontal(classes="action-bar"):
                        yield Button("Fetch My Repos", variant="primary", id="btn-fetch-gh")
                        yield Button("Import Selected", variant="success", id="btn-clone-gh")
                        yield Button("Import All", variant="warning", id="btn-import-all-gh")
                    yield DataTable(id="gh-repos-table")

                    yield Static("Log", classes="section-header")
                    yield RichLog(id="project-log", highlight=True, markup=True)

            # ── Custodian Tab ─────────────────────────────────
            with TabPane("Custodian", id="tab-custodian"):
                with Vertical(classes="tab-content"):
                    yield Static("Index Projects", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Select(
                            [],
                            prompt="Select project...",
                            id="custodian-project-select",
                            classes="project-selector",
                        )
                        yield Button("INDEX NOW", variant="primary", id="btn-index", classes="big-button")
                        yield Button("INDEX ALL", variant="warning", id="btn-index-all")

                    yield DataTable(id="project-status-table")
                    yield Static("Indexing Log", classes="section-header")
                    yield RichLog(id="custodian-log", highlight=True, markup=True)

            # ── Fossils Tab ───────────────────────────────────
            with TabPane("Fossils", id="tab-fossils"):
                with Vertical(classes="tab-content"):
                    yield Static("Fossil History", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Select(
                            [],
                            prompt="All projects",
                            id="fossil-project-select",
                            classes="project-selector",
                            allow_blank=True,
                        )
                    yield DataTable(id="fossil-table")
                    yield Static("Fossil Details", classes="section-header")
                    yield RichLog(id="fossil-detail", highlight=True, markup=True)

            # ── Detective Tab ─────────────────────────────────
            with TabPane("Detective", id="tab-detective"):
                with Vertical(classes="tab-content"):
                    yield Static("Detective Analysis", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Select(
                            [],
                            prompt="Select project...",
                            id="detective-project-select",
                            classes="project-selector",
                            allow_blank=True,
                        )
                        yield Button("Quick (Sonnet)", variant="primary", id="btn-detective-quick")
                        yield Button("Deep (Opus)", variant="error", id="btn-detective-deep")
                        yield Button("Refine Prompt", variant="default", id="btn-refine-prompt")

                    yield DataTable(id="insight-table")
                    yield Static("Insight Details", classes="section-header")
                    yield RichLog(id="insight-detail", highlight=True, markup=True)

            # ── Status Tab ────────────────────────────────────
            with TabPane("Status", id="tab-status"):
                with Vertical(classes="tab-content"):
                    with Horizontal(classes="action-bar"):
                        yield Button("Update Workbench", variant="warning", id="btn-update-workbench")
                        yield Static("", id="update-status-label")
                    yield Static("Ticker Indicators", classes="section-header")
                    with Horizontal(id="ticker-config-bar"):
                        yield Switch(value=True, id="ticker-indexing")
                        yield Label("Indexing")
                        yield Switch(value=True, id="ticker-sandbox")
                        yield Label("Sandbox")
                        yield Switch(value=True, id="ticker-fossils")
                        yield Label("Fossils")
                        yield Switch(value=True, id="ticker-shared_files")
                        yield Label("Shared")
                        yield Switch(value=True, id="ticker-projects")
                        yield Label("Projects")
                    yield Static("System Status", classes="section-header")
                    yield DataTable(id="status-projects-table")
                    yield Static("Database", classes="section-header")
                    yield Static("", id="db-stats")
                    yield Static("Recent MCP Queries", classes="section-header")
                    yield DataTable(id="query-log-table")

            # ── Editor Tab ────────────────────────────────────
            with TabPane("Editor", id="tab-editor"):
                with Vertical(classes="tab-content"):
                    with Horizontal(classes="action-bar"):
                        yield Select(
                            [],
                            prompt="Select project...",
                            id="editor-project-select",
                            classes="project-selector",
                        )
                        yield Button("Open Project", variant="primary", id="btn-open-project")
                    with Horizontal(id="editor-panel"):
                        yield WorkbenchDirectoryTree(
                            self._workbench_path,
                            id="editor-tree",
                        )
                        with Vertical(id="editor-right"):
                            with Horizontal(id="editor-toolbar"):
                                yield Static("No file open", id="editor-file-label")
                                yield Button("Save", variant="primary", id="btn-editor-save")
                                yield Button("Reload", variant="default", id="btn-editor-reload")
                            yield TextArea(
                                "",
                                id="editor-textarea",
                                show_line_numbers=True,
                                theme="monokai",
                            )
                    with Horizontal(id="git-toolbar"):
                        yield Button("Commit & Push", variant="warning", id="btn-git-push")
                        yield Button("Pull", variant="default", id="btn-git-pull")
                        yield Static("git: checking...", id="git-status-label")
                    with Vertical(id="chat-panel"):
                        with Horizontal(id="chat-toolbar"):
                            yield Button("New Session", variant="primary", id="btn-claude-new-session")
                            yield Button("Resume", variant="default", id="btn-claude-resume")
                            yield Static("No session", id="session-label")
                        yield RichLog(id="claude-chat-log", highlight=True, markup=True)
                        with Horizontal(id="chat-input-bar"):
                            yield Input(
                                placeholder="Ask OpenCode...",
                                id="chat-input",
                            )
                            yield Button("Send", variant="success", id="btn-claude-send")
                            yield Button("Stop", variant="error", id="btn-claude-stop")

            # ── Agent Factory Tab ─────────────────────────────
            with TabPane("Agent Factory", id="tab-agents"):
                with Vertical(classes="tab-content"):
                    yield Static("Agent Factory", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Button("New Agent", variant="success", id="btn-new-agent")
                        yield Button("New Pipeline", variant="primary", id="btn-new-pipeline")
                        yield Button("Run Selected", variant="warning", id="btn-run-agent")
                        yield Button("Delete Agent", variant="error", id="btn-delete-agent")
                    with Horizontal(id="agents-split"):
                        with Vertical(id="agents-left-panel"):
                            yield Static("Agents", classes="section-header")
                            yield DataTable(id="agents-table")
                            yield Static("Pipelines", classes="section-header")
                            yield DataTable(id="pipelines-table")
                        with Vertical(id="agents-right-panel"):
                            yield Static("Agent Editor", classes="section-header")
                            with Vertical(id="agent-editor-form"):
                                with Horizontal(classes="input-bar"):
                                    yield Input(placeholder="Agent name", id="agent-name-input", classes="name-input")
                                    if self._agent_model_error:
                                        yield Static(
                                            "Could not load model list — OpenCode unavailable. Restart Custodian after fixing.",
                                            id="agent-model-error",
                                        )
                                    else:
                                        yield Select(
                                            self._agent_model_options,
                                            value=self._agent_model_default,
                                            id="agent-model-select",
                                        )
                                    yield Select(
                                        [],
                                        prompt="Project (optional)",
                                        id="agent-project-select",
                                        allow_blank=True,
                                    )
                                yield Input(placeholder="Description", id="agent-desc-input")
                                yield TextArea(
                                    "",
                                    id="agent-prompt-textarea",
                                )
                                with Horizontal(classes="input-bar"):
                                    yield Input(placeholder="Max turns (default 20)", id="agent-turns-input")
                                    yield Button("Save Agent", variant="success", id="btn-save-agent")
                            yield Static("Run History", classes="section-header")
                            yield DataTable(id="runs-table")
                    yield Static("Reindex Requests", classes="section-header")
                    yield DataTable(id="reindex-table")
                    yield Static("Run Log", classes="section-header")
                    yield RichLog(id="agent-run-log", highlight=True, markup=True)

            # ── Alpha Builds Tab ──────────────────────────────
            with TabPane("Alpha Builds", id="tab-builds"):
                with Vertical(classes="tab-content"):
                    yield Static("Alpha Builds", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Select(
                            [],
                            prompt="Select project...",
                            id="builds-project-select",
                            classes="project-selector",
                        )
                        yield Button("Launch", variant="success", id="btn-launch-build")
                        yield Button("Stop", variant="error", id="btn-stop-build")
                        yield Button("Rebuild", variant="warning", id="btn-rebuild")
                    with Horizontal(classes="action-bar"):
                        yield Button("Shell", variant="primary", id="btn-shell-build")
                        yield Button("Run Tests", variant="default", id="btn-build-test")
                        yield Button("Install Deps", variant="default", id="btn-build-install")
                        yield Button("Logs", variant="default", id="btn-build-logs")
                    with Horizontal(id="builds-split"):
                        with VerticalScroll(id="builds-left-panel"):
                            pass  # Build cards populated dynamically
                        with Vertical(id="builds-right-panel"):
                            yield Static("No build selected", id="build-info-label")
                            yield RichLog(id="builds-log", highlight=True, markup=True)

            # ── Devices Tab ──────────────────────────────────────
            with TabPane("Devices", id="tab-devices"):
                with Vertical(classes="tab-content"):
                    yield Static("Paired Devices", classes="section-header")
                    yield DataTable(id="devices-table")
                    with Horizontal(classes="action-bar"):
                        yield Button("Generate Pairing Code", variant="success", id="btn-generate-pair-code")
                        yield Button("Remove Device", variant="error", id="btn-remove-device")
                    yield Static("", id="pairing-code-display")
                    yield RichLog(id="devices-log", highlight=True, markup=True)

            # ── Ticker Tab ──────────────────────────────────────
            with TabPane("Ticker", id="tab-ticker"):
                with Vertical(classes="tab-content"):
                    yield Static("Ticker Segments", classes="section-header")
                    yield DataTable(id="ticker-segments-table")
                    with Horizontal(classes="action-bar"):
                        yield Button("Move Up", variant="default", id="btn-ticker-up")
                        yield Button("Move Down", variant="default", id="btn-ticker-down")
                        yield Button("Toggle", variant="primary", id="btn-ticker-toggle")
                    yield Static("Overlay Settings", classes="section-header")
                    with Horizontal(classes="action-bar"):
                        yield Label("Speed:")
                        yield Input(value="50", id="ticker-speed", type="integer")
                        yield Label("Opacity %:")
                        yield Input(value="85", id="ticker-opacity", type="integer")
                        yield Label("Height px:")
                        yield Input(value="28", id="ticker-height", type="integer")
                    with Horizontal(classes="action-bar"):
                        yield Label("Position:")
                        yield Select(
                            [("Top", "top"), ("Bottom", "bottom")],
                            value="top",
                            id="ticker-position",
                        )
                        yield Label("Poll sec:")
                        yield Input(value="3", id="ticker-poll", type="integer")
                    with Horizontal(classes="action-bar"):
                        yield Button("Save Settings", variant="success", id="btn-ticker-save")
                        yield Button("Launch Overlay", variant="primary", id="btn-ticker-launch")
                        yield Button("Stop Overlay", variant="error", id="btn-ticker-stop")
                    yield Static("", id="ticker-preview")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize data on mount."""
        # Hide hierarchy tree until a card is clicked
        try:
            self.query_one("#hierarchy-tree", Tree).display = False
        except Exception:
            pass
        self._load_projects()
        self._refresh_projects_tab()
        self._refresh_custodian_tab()
        self._refresh_fossils_tab()
        self._refresh_detective_tab()
        self._refresh_status_tab()
        self._init_editor_tab()
        self._refresh_agents_tab()
        self._refresh_builds_tab()
        self._refresh_devices_tab()
        self._refresh_ticker_tab()

    def _load_projects(self):
        """Load projects and populate all select widgets."""
        self._projects = get_projects()
        options = [(p["name"], p["id"]) for p in self._projects]

        for select_id in ["custodian-project-select", "fossil-project-select", "detective-project-select", "editor-project-select", "agent-project-select", "builds-project-select"]:
            try:
                select = self.query_one(f"#{select_id}", Select)
                select.set_options(options)
            except Exception:
                pass

    # ── Projects Tab ──────────────────────────────────────────────────

    def _refresh_projects_tab(self):
        """Refresh the projects tab — cards + hierarchy."""
        self._refresh_project_cards()

    def _refresh_project_cards(self):
        """Build project cards inside #cards-panel."""
        try:
            panel = self.query_one("#cards-panel", VerticalScroll)
        except Exception:
            return

        # Remove existing card buttons (fire-and-forget async removal)
        for child in list(panel.children):
            child.remove()

        # Increment generation so new IDs never collide with still-removing widgets
        self._card_gen += 1
        gen = self._card_gen

        conn = get_db()
        rows = conn.execute(
            """SELECT p.id, p.name, p.stack, p.status, p.last_indexed,
                      COUNT(f.id) as fossil_count,
                      (SELECT COUNT(*) FROM symbols s
                       JOIN fossils f2 ON s.fossil_id = f2.id
                       WHERE f2.project_id = p.id) as symbol_count
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               GROUP BY p.id
               ORDER BY p.status, p.name"""
        ).fetchall()
        conn.close()

        for r in rows:
            pid = r["id"]
            name = r["name"]
            stack = r["stack"] or "unknown stack"
            indexed = r["last_indexed"]
            fossils = r["fossil_count"]
            symbols = r["symbol_count"]
            status = r["status"]

            if indexed:
                # Show short date
                date_short = indexed[:10] if len(indexed) >= 10 else indexed
                status_line = f"[green]\u25cf Indexed: {date_short}[/green]"
                css_class = "project-card indexed"
            else:
                status_line = "[dim]\u25cb Never indexed[/dim]"
                css_class = "project-card not-indexed"

            if status == "inactive":
                status_line += "  [yellow](inactive)[/yellow]"

            label = (
                f"[bold]{name}[/bold]\n"
                f"{stack}\n"
                f"{status_line}\n"
                f"Fossils: {fossils} \u00b7 Symbols: {symbols}"
            )

            btn = Button(label, id=f"btn-card-{gen}-{pid}", variant="default")
            btn.add_class(*css_class.split())
            panel.mount(btn)

        # Re-highlight selected card if still present
        if self._selected_card_project_id is not None:
            self._highlight_selected_card(self._selected_card_project_id)

    def _highlight_selected_card(self, project_id):
        """Visually highlight the selected project card."""
        try:
            panel = self.query_one("#cards-panel", VerticalScroll)
        except Exception:
            return

        for child in panel.children:
            if hasattr(child, "id") and child.id and child.id.startswith("btn-card-"):
                child.remove_class("selected")
                # ID format: btn-card-{gen}-{pid}
                if child.id.endswith(f"-{project_id}"):
                    child.add_class("selected")

    def _populate_hierarchy_tree(self, project_id):
        """Build the hierarchy tree for a project from fossil data."""
        try:
            tree_widget = self.query_one("#hierarchy-tree", Tree)
            placeholder = self.query_one("#hierarchy-placeholder", Static)
        except Exception:
            return

        tree_widget.clear()
        placeholder.display = False
        tree_widget.display = True

        conn = get_db()

        # Get project name
        proj = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not proj:
            conn.close()
            return

        project_name = proj["name"]
        tree_widget.root.set_label(project_name)

        # Get latest fossil
        fossil = conn.execute(
            """SELECT id, file_tree, dependencies, known_issues
               FROM fossils WHERE project_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()

        if not fossil:
            tree_widget.root.add_leaf("Not indexed yet \u2014 use Custodian tab to index this project")
            tree_widget.root.expand()
            conn.close()
            return

        fossil_id = fossil["id"]

        # Get symbols grouped by type
        symbols = conn.execute(
            """SELECT type, name, file_path, signature, description
               FROM symbols WHERE fossil_id = ?
               ORDER BY type, name""",
            (fossil_id,),
        ).fetchall()
        conn.close()

        # Group symbols by type
        sym_groups = {}
        for sym in symbols:
            stype = sym["type"]
            if stype not in sym_groups:
                sym_groups[stype] = []
            sym_groups[stype].append(sym)

        # Type display order and labels
        type_labels = {
            "component": "Components",
            "class": "Classes",
            "function": "Functions",
            "hook": "Hooks",
            "store": "Stores",
            "route": "Routes",
            "type": "Types/Interfaces",
        }

        for stype, label in type_labels.items():
            if stype in sym_groups:
                group = sym_groups[stype]
                branch = tree_widget.root.add(f"{label} ({len(group)})")
                for sym in group:
                    desc = sym["description"] or sym["file_path"] or ""
                    if desc:
                        branch.add_leaf(f"{sym['name']} \u2014 {desc}")
                    else:
                        branch.add_leaf(sym["name"])

        # Any remaining types not in the ordered list
        for stype, group in sym_groups.items():
            if stype not in type_labels:
                branch = tree_widget.root.add(f"{stype.title()} ({len(group)})")
                for sym in group:
                    desc = sym["description"] or sym["file_path"] or ""
                    if desc:
                        branch.add_leaf(f"{sym['name']} \u2014 {desc}")
                    else:
                        branch.add_leaf(sym["name"])

        # Dependencies
        if fossil["dependencies"]:
            try:
                deps = json.loads(fossil["dependencies"])
                if deps:
                    dep_branch = tree_widget.root.add(f"Dependencies ({len(deps)})")
                    for dep in deps:
                        if isinstance(dep, dict):
                            name = dep.get("name", "?")
                            version = dep.get("version", "")
                            purpose = dep.get("purpose", "")
                            label = f"{name} {version}".strip()
                            if purpose:
                                label += f" \u2014 {purpose}"
                            dep_branch.add_leaf(label)
                        else:
                            dep_branch.add_leaf(str(dep))
            except (json.JSONDecodeError, TypeError):
                pass

        # Known issues
        if fossil["known_issues"]:
            issues_text = fossil["known_issues"]
            try:
                issues = json.loads(issues_text)
                if issues:
                    issue_branch = tree_widget.root.add("Known Issues")
                    for issue in issues:
                        if isinstance(issue, dict):
                            issue_branch.add_leaf(issue.get("description", str(issue)))
                        else:
                            issue_branch.add_leaf(str(issue))
            except (json.JSONDecodeError, TypeError):
                # Plain text — split by lines
                lines = [l.strip() for l in issues_text.strip().split("\n") if l.strip()]
                if lines:
                    issue_branch = tree_widget.root.add("Known Issues")
                    for line in lines:
                        # Strip leading bullet chars
                        line = line.lstrip("- \u2022*")
                        if line:
                            issue_branch.add_leaf(line)

        tree_widget.root.expand()

    def _refresh_gh_repos_table(self, repos):
        """Populate the GitHub repos table."""
        table = self.query_one("#gh-repos-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Repository", "Description")
        table.cursor_type = "row"

        for i, repo in enumerate(repos):
            table.add_row(
                str(i + 1),
                repo.get("nameWithOwner", ""),
                (repo.get("description") or "")[:60],
            )

    # ── Custodian Tab ─────────────────────────────────────────────────

    def _refresh_custodian_tab(self):
        """Refresh the project status table."""
        table = self.query_one("#project-status-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Project", "Stack", "Status", "Last Indexed", "Fossils")

        conn = get_db()
        rows = conn.execute(
            """SELECT p.name, p.stack, p.status, p.last_indexed,
                      COUNT(f.id) as fossil_count
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               WHERE p.status = 'active'
               GROUP BY p.id
               ORDER BY p.name"""
        ).fetchall()
        conn.close()

        for r in rows:
            indexed = r["last_indexed"] or "never"
            table.add_row(r["name"], r["stack"] or "", r["status"], indexed, str(r["fossil_count"]))

    def _refresh_fossils_tab(self, project_id=None):
        """Refresh the fossil history table."""
        table = self.query_one("#fossil-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Project", "Version", "Date", "Summary")
        table.cursor_type = "row"

        fossils = get_fossils(project_id)
        for f in fossils[:50]:
            summary = (f["summary"] or "")[:80]
            table.add_row(
                str(f["id"]),
                f["project_name"],
                str(f["version"]),
                f["created_at"] or "",
                summary,
            )

    def _refresh_detective_tab(self, project_id=None):
        """Refresh the insights table."""
        table = self.query_one("#insight-table", DataTable)
        table.clear(columns=True)
        table.add_columns("ID", "Project", "Type", "Date", "Content Preview")
        table.cursor_type = "row"

        insights = get_insights(project_id)
        for i in insights:
            preview = (i["content"] or "")[:80]
            table.add_row(
                str(i["id"]),
                i.get("project_name") or "cross-project",
                i["insight_type"],
                i["created_at"] or "",
                preview,
            )

    def _load_ticker_config(self):
        """Load ticker config from DB and set Switch values."""
        keys = ["indexing", "sandbox", "fossils", "shared_files", "projects"]
        try:
            conn = get_db()
            # Seed defaults if table is empty
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ticker_config (key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)"
            )
            existing = conn.execute("SELECT COUNT(*) FROM ticker_config").fetchone()[0]
            if existing == 0:
                for k in keys:
                    conn.execute("INSERT INTO ticker_config (key, enabled) VALUES (?, 1)", (k,))
                conn.commit()
            rows = conn.execute("SELECT key, enabled FROM ticker_config").fetchall()
            conn.close()
            config = {r["key"]: bool(r["enabled"]) for r in rows}
            for k in keys:
                try:
                    sw = self.query_one(f"#ticker-{k}", Switch)
                    sw.value = config.get(k, True)
                except Exception:
                    pass
        except Exception:
            pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Handle ticker config switch toggles."""
        sw_id = event.switch.id or ""
        if not sw_id.startswith("ticker-"):
            return
        key = sw_id.removeprefix("ticker-")
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO ticker_config (key, enabled) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET enabled = ?",
                (key, int(event.value), int(event.value)),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _refresh_status_tab(self):
        """Refresh the status tab."""
        # Load ticker config switches from DB
        self._load_ticker_config()

        table = self.query_one("#status-projects-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Project", "Path", "Last Indexed", "Fossils", "Symbols")

        conn = get_db()
        rows = conn.execute(
            """SELECT p.name, p.path, p.last_indexed,
                      COUNT(f.id) as fossils,
                      (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as syms
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               WHERE p.status = 'active'
               GROUP BY p.id
               ORDER BY p.name"""
        ).fetchall()
        for r in rows:
            table.add_row(
                r["name"],
                r["path"],
                r["last_indexed"] or "never",
                str(r["fossils"]),
                str(r["syms"]),
            )

        stats = get_db_stats()
        db_text = (
            f"Size: {stats['db_size']/1024:.1f} KB  |  "
            f"Fossils: {stats['fossils']}  |  "
            f"Symbols: {stats['symbols']}  |  "
            f"Insights: {stats['insights']}  |  "
            f"Queries: {stats['queries']}  |  "
            f"Prompts: {stats['prompts']}"
        )
        self.query_one("#db-stats", Static).update(db_text)

        qtable = self.query_one("#query-log-table", DataTable)
        qtable.clear(columns=True)
        qtable.add_columns("Tool", "Project", "Time", "Params")

        qrows = conn.execute(
            "SELECT * FROM query_log ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        for q in qrows:
            qtable.add_row(
                q["tool_name"] or "",
                q["project_name"] or "",
                q["timestamp"] or "",
                (q["query_params"] or "")[:60],
            )

        conn.close()

    # ── Event Handlers ────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id

        # Projects tab — "Open in Editor" must be checked before generic btn-card- pattern
        if button_id == "btn-card-to-editor":
            if self._selected_card_project_id:
                try:
                    select = self.query_one("#editor-project-select", Select)
                    select.value = self._selected_card_project_id
                except Exception:
                    pass
                self._open_editor_project(self._selected_card_project_id)
                tabbed = self.query_one(TabbedContent)
                tabbed.active = "tab-editor"
            else:
                self.notify("Select a project card first", severity="warning")
        # Projects tab — card clicks
        elif button_id and button_id.startswith("btn-card-"):
            # ID format: btn-card-{gen}-{pid}
            project_id = int(button_id.split("-")[-1])
            self._selected_card_project_id = project_id
            self._highlight_selected_card(project_id)
            self._populate_hierarchy_tree(project_id)
        elif button_id == "btn-quick-import":
            self._do_quick_import()
        elif button_id == "btn-update-workbench":
            self._do_update_workbench()
        elif button_id == "btn-fetch-gh":
            self._do_fetch_gh_repos()
        elif button_id == "btn-clone-gh":
            # Capture cursor on main thread before dispatching to worker
            table = self.query_one("#gh-repos-table", DataTable)
            row_idx = table.cursor_row
            self._do_clone_gh_repo(row_idx)
        elif button_id == "btn-import-all-gh":
            self._do_import_all_gh_repos()
        elif button_id == "btn-remove-project":
            self._do_remove_project()
        elif button_id == "btn-reactivate-project":
            self._do_reactivate_project()
        # Custodian tab
        elif button_id == "btn-index":
            select = self.query_one("#custodian-project-select", Select)
            self._do_index_project(select.value)
        elif button_id == "btn-index-all":
            self._do_index_all()
        # Detective tab
        elif button_id == "btn-detective-quick":
            det_select = self.query_one("#detective-project-select", Select)
            self._do_detective("openai/gpt-5.4", det_select.value)
        elif button_id == "btn-detective-deep":
            det_select = self.query_one("#detective-project-select", Select)
            self._do_detective("openai/gpt-5.4", det_select.value)
        elif button_id == "btn-refine-prompt":
            self._do_refine_prompt()
        # Editor tab — project
        elif button_id == "btn-open-project":
            select = self.query_one("#editor-project-select", Select)
            if isinstance(select.value, int):
                self._open_editor_project(select.value)
            else:
                self.notify("Select a project first", severity="warning")
        # Editor tab — git
        elif button_id == "btn-git-push":
            self._do_git_commit_push()
        elif button_id == "btn-git-pull":
            self._do_git_pull()
        # Editor tab — file
        elif button_id == "btn-editor-save":
            self._save_current_file()
        elif button_id == "btn-editor-reload":
            self._reload_current_file()
        elif button_id == "btn-claude-send":
            self._do_send_claude_message()
        elif button_id == "btn-claude-stop":
            self._do_stop_claude()
        elif button_id == "btn-claude-new-session":
            self._do_new_claude_session()
        elif button_id == "btn-claude-resume":
            self._do_resume_claude_session()
        # Agent Factory tab
        elif button_id == "btn-new-agent":
            self._do_new_agent_form()
        elif button_id == "btn-save-agent":
            self._do_save_agent()
        elif button_id == "btn-delete-agent":
            self._do_delete_agent()
        elif button_id == "btn-run-agent":
            self._do_run_agent()
        elif button_id == "btn-new-pipeline":
            self._do_new_pipeline()
        # Reindex request buttons
        elif button_id and button_id.startswith("btn-approve-reindex-"):
            req_id = int(button_id.split("-")[-1])
            self._do_approve_reindex(req_id)
        elif button_id and button_id.startswith("btn-deny-reindex-"):
            req_id = int(button_id.split("-")[-1])
            self._do_deny_reindex(req_id)
        # Alpha Builds tab
        elif button_id == "btn-launch-build":
            select = self.query_one("#builds-project-select", Select)
            if isinstance(select.value, int):
                self._do_launch_build(select.value)
            else:
                self.notify("Select a project first", severity="warning")
        elif button_id == "btn-stop-build":
            self._do_stop_build()
        elif button_id == "btn-rebuild":
            self._do_rebuild()
        elif button_id == "btn-shell-build":
            self._do_shell_build()
        elif button_id == "btn-build-test":
            self._do_build_test()
        elif button_id == "btn-build-install":
            self._do_build_install()
        elif button_id == "btn-build-logs":
            self._do_build_logs()
        elif button_id and button_id.startswith("btn-build-card-"):
            build_id = int(button_id.split("-")[-1])
            self._selected_build_id = build_id
            self._show_build_info(build_id)
        # Devices tab
        elif button_id == "btn-generate-pair-code":
            self._do_generate_pair_code()
        elif button_id == "btn-remove-device":
            self._do_remove_device()
        # Ticker tab
        elif button_id == "btn-ticker-up":
            self._move_ticker_segment(-1)
        elif button_id == "btn-ticker-down":
            self._move_ticker_segment(1)
        elif button_id == "btn-ticker-toggle":
            self._toggle_ticker_segment()
        elif button_id == "btn-ticker-save":
            self._save_ticker_settings()
        elif button_id == "btn-ticker-launch":
            self._launch_ticker_overlay()
        elif button_id == "btn-ticker-stop":
            self._stop_ticker_overlay()

    def on_select_changed(self, event: Select.Changed) -> None:
        select_id = event.select.id

        if select_id == "fossil-project-select":
            self._refresh_fossils_tab(event.value if event.value != Select.BLANK else None)
        elif select_id == "detective-project-select":
            self._refresh_detective_tab(event.value if event.value != Select.BLANK else None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id

        if table_id == "fossil-table":
            self._show_fossil_detail(event)
        elif table_id == "insight-table":
            self._show_insight_detail(event)
        elif table_id == "gh-repos-table":
            # One-click import: clicking a row immediately clones + registers it
            row_idx = event.cursor_row
            self._do_clone_gh_repo(row_idx)
        elif table_id == "agents-table":
            self._on_agent_selected(event)
        elif table_id == "runs-table":
            self._on_run_selected(event)

    def _show_fossil_detail(self, event):
        """Show full fossil details in the detail pane."""
        detail = self.query_one("#fossil-detail", RichLog)
        detail.clear()

        try:
            row_data = event.data_table.get_row(event.row_key)
            fossil_id = int(row_data[0])
        except (IndexError, ValueError):
            return

        conn = get_db()
        fossil = conn.execute("SELECT * FROM fossils WHERE id = ?", (fossil_id,)).fetchone()
        if not fossil:
            conn.close()
            detail.write("Fossil not found")
            return

        symbol_count = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE fossil_id = ?", (fossil_id,)
        ).fetchone()[0]
        conn.close()

        detail.write(f"[bold]Fossil v{fossil['version']}[/bold] — {fossil['created_at']}")
        detail.write("")
        detail.write("[bold]Summary:[/bold]")
        detail.write(fossil["summary"] or "(none)")
        detail.write("")
        detail.write("[bold]Architecture:[/bold]")
        detail.write(fossil["architecture"] or "(none)")
        detail.write("")
        detail.write("[bold]Recent Changes:[/bold]")
        detail.write(fossil["recent_changes"] or "(none)")
        detail.write("")
        detail.write("[bold]Known Issues:[/bold]")
        detail.write(fossil["known_issues"] or "(none)")
        detail.write("")
        detail.write("[bold]Dependencies:[/bold]")
        try:
            deps = json.loads(fossil["dependencies"] or "[]")
            for d in deps:
                if isinstance(d, dict):
                    detail.write(f"  {d.get('name', '?')} {d.get('version', '')} — {d.get('purpose', '')}")
                else:
                    detail.write(f"  {d}")
        except json.JSONDecodeError:
            detail.write(fossil["dependencies"] or "(none)")
        detail.write("")
        detail.write(f"[bold]Symbols:[/bold] {symbol_count} indexed")

    def _show_insight_detail(self, event):
        """Show full insight details."""
        detail = self.query_one("#insight-detail", RichLog)
        detail.clear()

        try:
            row_data = event.data_table.get_row(event.row_key)
            insight_id = int(row_data[0])
        except (IndexError, ValueError):
            return

        conn = get_db()
        insight = conn.execute("SELECT * FROM detective_insights WHERE id = ?", (insight_id,)).fetchone()
        conn.close()

        if not insight:
            detail.write("Insight not found")
            return

        detail.write(f"[bold]{insight['insight_type'].upper()}[/bold] — {insight['created_at']}")
        detail.write(f"Model: {insight['model_used'] or 'unknown'}")
        if insight["projects_involved"]:
            detail.write(f"Projects: {insight['projects_involved']}")
        detail.write("")
        detail.write(insight["content"])

    # ── Update Worker ─────────────────────────────────────────────────

    @work(thread=True)
    def _do_update_workbench(self):
        """Pull latest NAI-Workbench changes from GitHub."""
        label = self.query_one("#update-status-label", Static)
        self.call_from_thread(label.update, "[bold blue]Pulling...[/bold blue]")
        self.call_from_thread(self.notify, "Updating from GitHub...")

        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=self._workbench_path,
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                msg = result.stderr.strip()[:80]
                self.call_from_thread(label.update, f"[red]{msg}[/red]")
                self.call_from_thread(self.notify, f"Update failed: {msg}", severity="error")
            elif "Already up to date" in output:
                self.call_from_thread(label.update, "[green]Already up to date[/green]")
                self.call_from_thread(self.notify, "Already up to date")
            else:
                self.call_from_thread(label.update, "[green]Updated! Restart for changes.[/green]")
                self.call_from_thread(self.notify, f"Updated! {output[:60]}")
        except Exception as e:
            self.call_from_thread(label.update, f"[red]Error: {e}[/red]")
            self.call_from_thread(self.notify, f"Update error: {e}", severity="error")

    # ── Quick Import Worker ───────────────────────────────────────────

    @work(thread=True)
    def _do_quick_import(self):
        """One-click import: paste URL or owner/repo, clone + register."""
        inp = self.query_one("#quick-import-input", Input)
        raw = inp.value.strip()
        log = self.query_one("#project-log", RichLog)

        if not raw:
            self.call_from_thread(self.notify, "Paste a GitHub URL or owner/repo first", severity="warning")
            return

        # Parse: accept full URLs, owner/repo, or just repo name
        # https://github.com/owner/repo.git → owner/repo
        # https://github.com/owner/repo → owner/repo
        # owner/repo → owner/repo
        import re as _re
        m = _re.match(r"(?:https?://github\.com/)?([^/\s]+/[^/\s]+?)(?:\.git)?/?$", raw)
        if m:
            repo_full = m.group(1)
        else:
            self.call_from_thread(self.notify, "Invalid format. Use: owner/repo or full GitHub URL", severity="error")
            log.write(f"[red]Invalid input: {raw}[/red]")
            return

        repo_name = repo_full.split("/")[-1]
        slug = slugify(repo_name)
        repo_url = f"https://github.com/{repo_full}.git"
        target = os.path.join(PROJECTS_DIR, repo_name)

        log.write(f"[bold]Quick import: {repo_full} → {target}[/bold]")
        self.call_from_thread(self.notify, f"Importing {repo_name}...")

        if os.path.isdir(target):
            log.write(f"[yellow]{repo_name} already exists — pulling latest...[/yellow]")
            try:
                result = subprocess.run(
                    ["git", "-C", target, "pull"],
                    capture_output=True, text=True, timeout=60,
                )
                log.write(result.stdout.strip() or result.stderr.strip() or "Up to date")
            except Exception as e:
                log.write(f"[red]Pull failed: {e}[/red]")
        else:
            log.write(f"[bold blue]Cloning {repo_url}...[/bold blue]")
            try:
                os.makedirs(PROJECTS_DIR, exist_ok=True)
                result = subprocess.run(
                    ["git", "clone", "--progress", repo_url, target],
                    capture_output=True, text=True, timeout=120,
                )
                if result.stderr.strip():
                    for line in result.stderr.strip().split("\n"):
                        log.write(line)
                if result.returncode != 0:
                    log.write("[bold red]Clone failed![/bold red]")
                    self.call_from_thread(self.notify, "Clone failed!", severity="error")
                    return
                log.write(f"[green]Cloned to {target}[/green]")
            except subprocess.TimeoutExpired:
                log.write("[red]Clone timed out (120s)[/red]")
                self.call_from_thread(self.notify, "Clone timed out", severity="error")
                return
            except Exception as e:
                log.write(f"[red]Clone error: {e}[/red]")
                self.call_from_thread(self.notify, f"Clone error: {e}", severity="error")
                return

        # Detect stack and register
        stack = detect_stack(target)
        log.write(f"Detected stack: {stack or '(unknown)'}")
        register_project(slug, target, stack)
        log.write(f"[bold green]Registered '{slug}' in custodian DB[/bold green]")

        # Clear input and refresh
        self.call_from_thread(setattr, inp, "value", "")
        self.call_from_thread(self._load_projects)
        self.call_from_thread(self._refresh_projects_tab)
        self.call_from_thread(self._refresh_custodian_tab)
        self.call_from_thread(self.notify, f"Imported {slug}")

    # ── Projects Workers ──────────────────────────────────────────────

    @work(thread=True)
    def _do_fetch_gh_repos(self):
        """Fetch GitHub repos via gh CLI."""
        log = self.query_one("#project-log", RichLog)
        log.write("[bold blue]Fetching GitHub repos...[/bold blue]")
        self.call_from_thread(self.notify, "Fetching repos from GitHub...")

        try:
            result = subprocess.run(
                ["gh", "repo", "list", "--limit", "50", "--json", "nameWithOwner,description"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.write(f"[red]gh failed: {result.stderr.strip()}[/red]")
                self.call_from_thread(self.notify, f"gh failed: {result.stderr.strip()[:60]}", severity="error")
                return

            self._gh_repos = json.loads(result.stdout)
            log.write(f"[green]Found {len(self._gh_repos)} repos[/green]")
            self.call_from_thread(self._refresh_gh_repos_table, self._gh_repos)
            self.call_from_thread(self.notify, f"Found {len(self._gh_repos)} repos — select one and click Import")

        except FileNotFoundError:
            log.write("[red]gh CLI not found. Install: https://cli.github.com[/red]")
            self.call_from_thread(self.notify, "gh CLI not found", severity="error")
        except subprocess.TimeoutExpired:
            log.write("[red]Timed out fetching repos[/red]")
            self.call_from_thread(self.notify, "Timed out fetching repos", severity="error")
        except Exception as e:
            log.write(f"[red]Error: {e}[/red]")
            self.call_from_thread(self.notify, f"Error: {e}", severity="error")

    @work(thread=True)
    def _do_clone_gh_repo(self, row_idx):
        """Clone selected GitHub repo and register it."""
        log = self.query_one("#project-log", RichLog)

        if not self._gh_repos:
            log.write("[red]Fetch repos first (click 'Fetch My Repos')[/red]")
            self.call_from_thread(self.notify, "Fetch repos first!", severity="warning")
            return

        if row_idx is None or row_idx < 0 or row_idx >= len(self._gh_repos):
            log.write(f"[red]Invalid selection (row {row_idx}, have {len(self._gh_repos)} repos). Click a repo row first.[/red]")
            self.call_from_thread(self.notify, "Select a repo row first", severity="warning")
            return

        repo = self._gh_repos[row_idx]
        repo_full = repo["nameWithOwner"]
        repo_name = repo_full.split("/")[-1]
        slug = slugify(repo_name)
        repo_url = f"https://github.com/{repo_full}.git"
        target = os.path.join(PROJECTS_DIR, repo_name)

        log.write(f"[bold]Selected: {repo_full} → {target}[/bold]")
        self.call_from_thread(self.notify, f"Importing {repo_name}...")

        if os.path.isdir(target):
            log.write(f"[yellow]{repo_name} already exists at {target}[/yellow]")
            log.write("[bold]Pulling latest...[/bold]")
            self.call_from_thread(self.notify, f"{repo_name} exists — pulling latest...")
            try:
                result = subprocess.run(
                    ["git", "-C", target, "pull"],
                    capture_output=True, text=True, timeout=60,
                )
                log.write(result.stdout.strip() or result.stderr.strip() or "Up to date")
            except Exception as e:
                log.write(f"[red]Pull failed: {e}[/red]")
                self.call_from_thread(self.notify, f"Pull failed: {e}", severity="error")
        else:
            log.write(f"[bold blue]Cloning {repo_url}...[/bold blue]")
            self.call_from_thread(self.notify, f"Cloning {repo_name}... (this may take a moment)")
            try:
                os.makedirs(PROJECTS_DIR, exist_ok=True)
                result = subprocess.run(
                    ["git", "clone", "--progress", repo_url, target],
                    capture_output=True, text=True, timeout=120,
                )
                # git clone writes progress to stderr
                if result.stdout.strip():
                    log.write(result.stdout.strip())
                if result.stderr.strip():
                    for line in result.stderr.strip().split("\n"):
                        log.write(line)

                if result.returncode != 0:
                    log.write("[bold red]Clone failed![/bold red]")
                    self.call_from_thread(self.notify, "Clone failed!", severity="error")
                    return

                log.write(f"[green]Cloned to {target}[/green]")
            except subprocess.TimeoutExpired:
                log.write("[red]Clone timed out (120s)[/red]")
                self.call_from_thread(self.notify, "Clone timed out (120s)", severity="error")
                return
            except Exception as e:
                log.write(f"[red]Clone error: {type(e).__name__}: {e}[/red]")
                self.call_from_thread(self.notify, f"Clone error: {e}", severity="error")
                return

        # Detect stack
        stack = detect_stack(target)
        log.write(f"Detected stack: {stack or '(unknown)'}")

        # Register in DB
        register_project(slug, target, stack)
        log.write(f"[bold green]Registered '{slug}' in custodian DB[/bold green]")

        # Refresh
        self.call_from_thread(self._load_projects)
        self.call_from_thread(self._refresh_projects_tab)
        self.call_from_thread(self._refresh_custodian_tab)

        self.call_from_thread(self.notify, f"Imported {slug}")

    @work(thread=True)
    def _do_import_all_gh_repos(self):
        """Fetch all GitHub repos and import every one."""
        log = self.query_one("#project-log", RichLog)
        log.write("[bold blue]Fetching all GitHub repos...[/bold blue]")
        self.call_from_thread(self.notify, "Fetching all repos from GitHub...")

        # Fetch repo list
        try:
            result = subprocess.run(
                ["gh", "repo", "list", "--limit", "50", "--json", "nameWithOwner,description"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.write(f"[red]gh failed: {result.stderr.strip()}[/red]")
                self.call_from_thread(self.notify, f"gh failed: {result.stderr.strip()[:60]}", severity="error")
                return

            repos = json.loads(result.stdout)
            self._gh_repos = repos
            self.call_from_thread(self._refresh_gh_repos_table, repos)
        except FileNotFoundError:
            log.write("[red]gh CLI not found[/red]")
            self.call_from_thread(self.notify, "gh CLI not found", severity="error")
            return
        except Exception as e:
            log.write(f"[red]Error fetching repos: {e}[/red]")
            self.call_from_thread(self.notify, f"Error: {e}", severity="error")
            return

        total = len(repos)
        log.write(f"[green]Found {total} repos — importing all...[/green]")
        self.call_from_thread(self.notify, f"Importing {total} repos...")

        imported = 0
        skipped = 0
        failed = 0

        for i, repo in enumerate(repos, 1):
            repo_full = repo["nameWithOwner"]
            repo_name = repo_full.split("/")[-1]
            slug = slugify(repo_name)
            repo_url = f"https://github.com/{repo_full}.git"
            target = os.path.join(PROJECTS_DIR, repo_name)

            log.write(f"\n[bold]({i}/{total}) {repo_full}[/bold]")

            if os.path.isdir(target):
                log.write(f"  [yellow]Already exists — pulling latest[/yellow]")
                try:
                    subprocess.run(
                        ["git", "-C", target, "pull"],
                        capture_output=True, text=True, timeout=60,
                    )
                except Exception:
                    pass
                skipped += 1
            else:
                log.write(f"  [blue]Cloning...[/blue]")
                try:
                    os.makedirs(PROJECTS_DIR, exist_ok=True)
                    result = subprocess.run(
                        ["git", "clone", "--progress", repo_url, target],
                        capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode != 0:
                        log.write(f"  [red]Clone failed[/red]")
                        failed += 1
                        continue
                    log.write(f"  [green]Cloned[/green]")
                except subprocess.TimeoutExpired:
                    log.write(f"  [red]Timed out[/red]")
                    failed += 1
                    continue
                except Exception as e:
                    log.write(f"  [red]{e}[/red]")
                    failed += 1
                    continue

            # Detect stack & register
            stack = detect_stack(target)
            register_project(slug, target, stack)
            log.write(f"  Registered: {slug} ({stack or 'unknown stack'})")
            imported += 1

            # Update notification every 5 repos
            if i % 5 == 0:
                self.call_from_thread(self.notify, f"Progress: {i}/{total}...")

        # Final refresh
        self.call_from_thread(self._load_projects)
        self.call_from_thread(self._refresh_projects_tab)
        self.call_from_thread(self._refresh_custodian_tab)

        summary = f"Done! Imported: {imported}, Skipped: {skipped}, Failed: {failed}"
        log.write(f"\n[bold green]{summary}[/bold green]")
        self.call_from_thread(self.notify, summary)

    def _do_remove_project(self):
        """Deactivate the selected project."""
        log = self.query_one("#project-log", RichLog)

        if self._selected_card_project_id is None:
            log.write("[red]Click a project card first[/red]")
            self.notify("Click a project card first", severity="warning")
            return

        project_id = self._selected_card_project_id
        conn = get_db()
        proj = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            log.write("[red]Project not found[/red]")
            conn.close()
            return

        project_name = proj["name"]
        conn.execute("UPDATE projects SET status = 'inactive' WHERE id = ?", (project_id,))
        conn.commit()
        conn.close()

        log.write(f"[yellow]Deactivated '{project_name}' (use Reactivate to undo)[/yellow]")
        self._load_projects()
        self._refresh_projects_tab()
        self._refresh_custodian_tab()

    def _do_reactivate_project(self):
        """Reactivate a deactivated project."""
        log = self.query_one("#project-log", RichLog)

        if self._selected_card_project_id is None:
            log.write("[red]Click a project card first[/red]")
            self.notify("Click a project card first", severity="warning")
            return

        project_id = self._selected_card_project_id
        conn = get_db()
        proj = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            log.write("[red]Project not found[/red]")
            conn.close()
            return

        project_name = proj["name"]
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
        conn.commit()
        conn.close()

        log.write(f"[green]Reactivated '{project_name}'[/green]")
        self._load_projects()
        self._refresh_projects_tab()
        self._refresh_custodian_tab()

    # ── Custodian Workers ─────────────────────────────────────────────

    @work(thread=True)
    def _do_index_project(self, selected_value):
        """Run custodian indexing for selected project."""
        if selected_value == Select.BLANK:
            log = self.query_one("#custodian-log", RichLog)
            log.write("[red]Select a project first[/red]")
            return

        project_id = selected_value
        conn = get_db()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        conn.close()

        if not project:
            return

        log = self.query_one("#custodian-log", RichLog)
        log.write(f"\n[bold blue]Starting index for {project['name']}...[/bold blue]")

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_project.sh")

        try:
            process = subprocess.Popen(
                ["bash", script, project["name"], project["path"]],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    log.write(line)

            process.wait()

            if process.returncode == 0:
                log.write(f"[bold green]Index complete for {project['name']}[/bold green]")
            else:
                log.write(f"[bold red]Index failed (exit code {process.returncode})[/bold red]")

        except Exception as e:
            log.write(f"[bold red]Error: {e}[/bold red]")

        self.call_from_thread(self._refresh_custodian_tab)
        self.call_from_thread(self._refresh_projects_tab)
        self.call_from_thread(self._refresh_fossils_tab)
        self.call_from_thread(self._refresh_status_tab)

    @work(thread=True)
    def _do_index_all(self):
        """Index all active projects."""
        log = self.query_one("#custodian-log", RichLog)
        log.write("\n[bold yellow]Indexing ALL projects...[/bold yellow]")

        projects = get_projects()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_project.sh")

        for project in projects:
            log.write(f"\n[bold blue]=== {project['name']} ===[/bold blue]")
            try:
                process = subprocess.Popen(
                    ["bash", script, project["name"], project["path"]],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in iter(process.stdout.readline, ""):
                    line = line.rstrip()
                    if line:
                        log.write(line)
                process.wait()
            except Exception as e:
                log.write(f"[red]Error indexing {project['name']}: {e}[/red]")

        log.write("\n[bold green]All projects indexed[/bold green]")
        self.call_from_thread(self._refresh_custodian_tab)
        self.call_from_thread(self._refresh_projects_tab)
        self.call_from_thread(self._refresh_fossils_tab)
        self.call_from_thread(self._refresh_status_tab)

    # ── Detective Workers ─────────────────────────────────────────────

    @work(thread=True)
    def _do_detective(self, model, selected_value):
        """Run detective analysis."""
        log = self.query_one("#insight-detail", RichLog)
        log.clear()
        log.write(f"[bold]Running detective ({model})...[/bold]")

        project_name = None
        if selected_value != Select.BLANK:
            conn = get_db()
            project = conn.execute("SELECT name FROM projects WHERE id = ?", (selected_value,)).fetchone()
            conn.close()
            if project:
                project_name = project["name"]

        detective_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detective.py")

        try:
            args = ["python", detective_script, "--model", model]
            if project_name:
                args.extend(["--project", project_name])

            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    log.write(line)

            process.wait()

            if process.returncode == 0:
                log.write("[bold green]Detective analysis complete[/bold green]")
            else:
                log.write(f"[bold red]Detective failed (exit {process.returncode})[/bold red]")

        except Exception as e:
            log.write(f"[bold red]Error: {e}[/bold red]")

        self.call_from_thread(self._refresh_detective_tab)

    @work(thread=True)
    def _do_refine_prompt(self):
        """Run detective prompt refinement."""
        log = self.query_one("#insight-detail", RichLog)
        log.clear()
        log.write("[bold]Running prompt refinement...[/bold]")

        detective_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detective.py")

        try:
            process = subprocess.Popen(
                ["python", detective_script, "--refine-prompt"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    log.write(line)

            process.wait()

        except Exception as e:
            log.write(f"[bold red]Error: {e}[/bold red]")

    # ── Agent Factory Tab ──────────────────────────────────────────

    def _refresh_agents_tab(self):
        """Refresh all Agent Factory data tables."""
        # Agents table
        try:
            table = self.query_one("#agents-table", DataTable)
            table.clear(columns=True)
            table.add_columns("ID", "Name", "Model", "Project", "Status")
            table.cursor_type = "row"
            for a in get_agents():
                table.add_row(
                    str(a["id"]), a["name"], a["model"],
                    a.get("project_name") or "", a["status"],
                )
        except Exception:
            pass

        # Pipelines table
        try:
            ptable = self.query_one("#pipelines-table", DataTable)
            ptable.clear(columns=True)
            ptable.add_columns("ID", "Name", "Steps", "Schedule")
            ptable.cursor_type = "row"
            for p in get_pipelines():
                steps = json.loads(p["steps"]) if p["steps"] else []
                ptable.add_row(
                    str(p["id"]), p["name"], str(len(steps)),
                    p["schedule"] or "manual",
                )
        except Exception:
            pass

        # Runs table
        try:
            rtable = self.query_one("#runs-table", DataTable)
            rtable.clear(columns=True)
            rtable.add_columns("ID", "Agent", "Status", "Started", "Tokens")
            rtable.cursor_type = "row"
            for r in get_agent_runs(limit=15):
                rtable.add_row(
                    str(r["id"]), r["agent_name"], r["status"],
                    (r["started_at"] or "")[:16], str(r.get("tokens_used") or ""),
                )
        except Exception:
            pass

        # Reindex requests
        self._refresh_reindex_requests()

    def _refresh_reindex_requests(self):
        """Refresh the reindex requests table."""
        try:
            table = self.query_one("#reindex-table", DataTable)
            table.clear(columns=True)
            table.add_columns("ID", "Project", "Reason", "Requested", "Actions")
            table.cursor_type = "row"
            for req in get_pending_reindex_requests():
                table.add_row(
                    str(req["id"]),
                    req["project_name"],
                    (req["reason"] or "")[:50],
                    (req["requested_at"] or "")[:16],
                    "Select row → use buttons below",
                )
        except Exception:
            pass

    def _on_agent_selected(self, event):
        """Load agent data into the editor form."""
        try:
            row_data = event.data_table.get_row(event.row_key)
            agent_id = int(row_data[0])
        except (IndexError, ValueError):
            return

        agent = get_agent(agent_id)
        if not agent:
            return

        self._selected_agent_id = agent_id

        try:
            self.query_one("#agent-name-input", Input).value = agent["name"]
            self.query_one("#agent-desc-input", Input).value = agent["description"] or ""
            self.query_one("#agent-prompt-textarea", TextArea).load_text(
                agent["system_prompt"] or ""
            )
            if not self._agent_model_error:
                model_select = self.query_one("#agent-model-select", Select)
                model_select.set_options(
                    _agent_model_options_with_current(self._agent_model_options, agent["model"])
                )
                model_select.value = agent["model"]
            turns_input = self.query_one("#agent-turns-input", Input)
            turns_input.value = str(agent["max_turns"] or 20)
            if agent["project_id"]:
                self.query_one("#agent-project-select", Select).value = agent["project_id"]
        except Exception:
            pass

    def _on_run_selected(self, event):
        """Show run output in the log."""
        try:
            row_data = event.data_table.get_row(event.row_key)
            run_id = int(row_data[0])
        except (IndexError, ValueError):
            return

        conn = get_db()
        run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()

        if not run:
            return

        log = self.query_one("#agent-run-log", RichLog)
        log.clear()
        log.write(f"[bold]Run #{run['id']}[/bold] — {run['status']}")
        log.write(f"Started: {run['started_at']}")
        if run["finished_at"]:
            log.write(f"Finished: {run['finished_at']}")
        if run["tokens_used"]:
            log.write(f"Tokens: {run['tokens_used']}")
        if run["error"]:
            log.write(f"[red]Error: {run['error']}[/red]")
        log.write("")
        log.write("[bold]Output:[/bold]")
        log.write(run["output"] or "(no output)")

    def _do_new_agent_form(self):
        """Clear the agent editor form for a new agent."""
        self._selected_agent_id = None
        try:
            self.query_one("#agent-name-input", Input).value = ""
            self.query_one("#agent-desc-input", Input).value = ""
            self.query_one("#agent-prompt-textarea", TextArea).load_text("")
            if self._agent_model_default:
                model_select = self.query_one("#agent-model-select", Select)
                model_select.set_options(self._agent_model_options)
                model_select.value = self._agent_model_default
            self.query_one("#agent-turns-input", Input).value = "20"
        except Exception:
            pass
        self.notify("New agent — fill in the form and Save")

    def _do_save_agent(self):
        """Save the agent from the editor form."""
        try:
            name = self.query_one("#agent-name-input", Input).value.strip()
            desc = self.query_one("#agent-desc-input", Input).value.strip()
            prompt = self.query_one("#agent-prompt-textarea", TextArea).text.strip()
            if self._agent_model_error:
                self.notify(f"Model list unavailable: {self._agent_model_error}", severity="error")
                return
            model = self.query_one("#agent-model-select", Select).value
            turns_raw = self.query_one("#agent-turns-input", Input).value.strip()
            max_turns = int(turns_raw) if turns_raw else 20
            proj_select = self.query_one("#agent-project-select", Select)
            project_id = proj_select.value if proj_select.value != Select.BLANK else None
        except Exception as e:
            self.notify(f"Form error: {e}", severity="error")
            return

        if not name:
            self.notify("Agent name is required", severity="warning")
            return
        if not prompt:
            self.notify("System prompt is required", severity="warning")
            return

        try:
            save_agent(
                name=name, system_prompt=prompt, description=desc,
                model=model, project_id=project_id, max_turns=max_turns,
                agent_id=self._selected_agent_id,
            )
            self.notify(f"Agent '{name}' saved")
            self._refresh_agents_tab()
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error")

    def _do_delete_agent(self):
        """Delete the selected agent."""
        if not self._selected_agent_id:
            self.notify("Select an agent first", severity="warning")
            return
        agent = get_agent(self._selected_agent_id)
        if agent:
            delete_agent(self._selected_agent_id)
            self.notify(f"Deleted agent '{agent['name']}'")
            self._selected_agent_id = None
            self._refresh_agents_tab()

    @work(thread=True)
    def _do_run_agent(self):
        """Run the selected agent via Claude CLI subprocess."""
        if not self._selected_agent_id:
            self.call_from_thread(self.notify, "Select an agent first", severity="warning")
            return

        if self._agent_running:
            self.call_from_thread(self.notify, "Agent already running", severity="warning")
            return

        agent = get_agent(self._selected_agent_id)
        if not agent:
            self.call_from_thread(self.notify, "Agent not found", severity="error")
            return

        log = self.query_one("#agent-run-log", RichLog)
        log.clear()
        log.write(f"[bold blue]Running agent: {agent['name']} ({agent['model']})...[/bold blue]")
        self._agent_running = True

        run_id = create_agent_run(agent["id"], input_text=agent["system_prompt"])

        # Set working directory to project path if bound
        cwd = self._workbench_path
        if agent.get("project_id"):
            conn = get_db()
            proj = conn.execute(
                "SELECT path FROM projects WHERE id = ?", (agent["project_id"],)
            ).fetchone()
            conn.close()
            if proj:
                cwd = _to_native_path(proj["path"])

        try:
            prompt = agent.get("description") or f"Execute your purpose as {agent['name']}"
            result = run_opencode(
                prompt=prompt,
                model=agent["model"],
                system_prompt=agent["system_prompt"],
                project_dir=cwd,
                max_turns=agent.get("max_turns"),
                timeout=600,
            )

            for line in result.text.split("\n"):
                if line.strip():
                    log.write(line)

            meta = []
            if result.tokens_used:
                meta.append(f"{result.tokens_used} tokens")
            if result.cost_usd is not None:
                meta.append(f"${result.cost_usd:.4f}")
            if meta:
                log.write(f"[dim]({', '.join(meta)})[/dim]")

            complete_agent_run(
                run_id,
                status="completed",
                output=result.text,
                tokens_used=result.tokens_used,
            )
            log.write(f"[bold green]Agent run complete[/bold green]")

        except OpenCodeRunnerError as e:
            if e.stderr:
                log.write(f"[red]{e.stderr}[/red]")
            complete_agent_run(
                run_id,
                status="failed",
                output=e.text,
                tokens_used=e.tokens_used,
                error=e.stderr or str(e),
            )
        except Exception as e:
            log.write(f"[red]Error: {type(e).__name__}: {e}[/red]")
            complete_agent_run(run_id, status="failed", error=str(e))
        finally:
            self._agent_running = False
            self.call_from_thread(self._refresh_agents_tab)

    def _do_new_pipeline(self):
        """Create a new pipeline (placeholder — opens log with instructions)."""
        log = self.query_one("#agent-run-log", RichLog)
        log.clear()
        log.write("[bold]Pipeline creation[/bold]")
        log.write("")
        log.write("Pipelines chain multiple agents together.")
        log.write("To create a pipeline, first create agents, then define steps.")
        log.write("")
        log.write("[dim]Pipeline editor coming in a future update.[/dim]")
        log.write("[dim]For now, use the DB directly or Claude CLI to create pipelines.[/dim]")
        self.notify("Pipeline editor coming soon")

    def _do_approve_reindex(self, request_id):
        """Approve a reindex request and trigger indexing."""
        req = approve_reindex(request_id)
        if not req:
            self.notify("Request not found", severity="error")
            return

        log = self.query_one("#agent-run-log", RichLog)
        log.write(f"[bold green]Approved reindex for {req['project_name']}[/bold green]")
        self.notify(f"Approved reindex for {req['project_name']}")
        self._refresh_reindex_requests()

        # Trigger indexing
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_project.sh")
        try:
            subprocess.Popen(
                ["bash", script, req["project_name"], req["project_path"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            log.write(f"[bold blue]Indexing started for {req['project_name']}[/bold blue]")
        except Exception as e:
            log.write(f"[red]Failed to start indexing: {e}[/red]")

    def _do_deny_reindex(self, request_id):
        """Deny a reindex request."""
        deny_reindex(request_id)
        self.notify("Reindex request denied")
        self._refresh_reindex_requests()

        log = self.query_one("#agent-run-log", RichLog)
        log.write("[yellow]Reindex request denied[/yellow]")

    # ── Alpha Builds Tab ─────────────────────────────────────────

    def _refresh_builds_tab(self):
        """Refresh the Alpha Builds tab — build cards."""
        self._refresh_build_cards()

    def _refresh_build_cards(self):
        """Build cards inside #builds-left-panel."""
        try:
            panel = self.query_one("#builds-left-panel", VerticalScroll)
        except Exception:
            return

        for child in list(panel.children):
            child.remove()

        self._build_card_gen += 1
        gen = self._build_card_gen

        builds = get_alpha_builds()
        if not builds:
            panel.mount(Static("[dim]No builds yet — select a project and Launch Build[/dim]"))
            return

        for b in builds:
            status = b["status"]
            name = b.get("project_name") or b.get("container_name") or "?"
            container = (b.get("container_id") or "")[:12]
            image = b.get("image") or ""

            if status == "running":
                status_line = f"[green]\u25cf {status}[/green]"
                css_class = "build-card running"
            elif status == "building":
                status_line = f"[yellow]\u25cf {status}[/yellow]"
                css_class = "build-card building"
            elif status == "failed":
                status_line = f"[red]\u25cf {status}[/red]"
                css_class = "build-card failed"
            else:
                status_line = f"[dim]\u25cb {status}[/dim]"
                css_class = "build-card stopped"

            label = (
                f"[bold]{name}[/bold]\n"
                f"{image}\n"
                f"{status_line}\n"
                f"Container: {container or 'none'}"
            )

            btn = Button(label, id=f"btn-build-card-{gen}-{b['id']}", variant="default")
            btn.add_class(*css_class.split())
            panel.mount(btn)

    def _show_build_info(self, build_id):
        """Show build details in the right panel."""
        conn = get_db()
        build = conn.execute(
            """SELECT ab.*, p.name as project_name
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.id = ?""",
            (build_id,),
        ).fetchone()
        conn.close()

        if not build:
            return

        label = self.query_one("#build-info-label", Static)
        parts = [
            f"[bold]{build['project_name']}[/bold]",
            f"Status: {build['status']}",
            f"Image: {build['image'] or 'none'}",
            f"Container: {(build['container_id'] or '')[:12] or 'none'}",
            f"Command: {build['command'] or 'none'}",
        ]
        if build["ports"]:
            parts.append(f"Ports: {build['ports']}")
        label.update(" | ".join(parts))

    @work(thread=True)
    def _do_launch_build(self, project_id):
        """Launch a Docker container for the selected project."""
        conn = get_db()
        proj = conn.execute(
            "SELECT name, path, stack FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.close()

        if not proj:
            self.call_from_thread(self.notify, "Project not found", severity="error")
            return

        project_name = proj["name"]
        project_path = _to_native_path(proj["path"])
        log = self.query_one("#builds-log", RichLog)
        log.clear()
        log.write(f"[bold blue]Launching build for {project_name}...[/bold blue]")

        container_name = f"alpha-{project_name}"

        # Check if container already exists
        result = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            log.write(f"[yellow]Container {container_name} already exists — starting...[/yellow]")
            subprocess.run(["docker", "start", container_name], capture_output=True)
            # Get container ID
            cid = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", container_name],
                capture_output=True, text=True,
            ).stdout.strip()
            save_alpha_build(
                project_id=project_id, container_id=cid[:12],
                container_name=container_name, status="running",
                image="(existing)",
            )
            log.write(f"[bold green]Container started: {container_name}[/bold green]")
            self.call_from_thread(self._refresh_builds_tab)
            self.call_from_thread(self.notify, f"Started {container_name}")
            return

        # Check for devcontainer config
        devcontainer_path = os.path.join(project_path, ".devcontainer")
        if os.path.isdir(devcontainer_path):
            dockerfile = os.path.join(devcontainer_path, "Dockerfile")
            if os.path.isfile(dockerfile):
                image_name = f"alpha-{project_name}:latest"
                log.write(f"[bold blue]Building from devcontainer...[/bold blue]")
                save_alpha_build(
                    project_id=project_id, container_name=container_name,
                    image=image_name, status="building",
                )
                self.call_from_thread(self._refresh_builds_tab)

                build_result = subprocess.run(
                    ["docker", "build", "-t", image_name,
                     "-f", dockerfile, project_path],
                    capture_output=True, text=True, timeout=300,
                )
                if build_result.returncode != 0:
                    log.write(f"[red]Build failed:[/red]")
                    for line in build_result.stderr.strip().split("\n")[-10:]:
                        log.write(f"  [red]{line}[/red]")
                    save_alpha_build(
                        project_id=project_id, container_name=container_name,
                        image=image_name, status="failed",
                        build_log=build_result.stderr[-2000:],
                    )
                    self.call_from_thread(self._refresh_builds_tab)
                    return
                log.write(f"[green]Built image: {image_name}[/green]")
            else:
                image_name = "python:3.12" if "Python" in (proj["stack"] or "") else "node:22"
        else:
            # Default image based on stack
            stack = proj["stack"] or ""
            if "Python" in stack:
                image_name = "python:3.12"
            elif "Node" in stack or "React" in stack or "Next" in stack:
                image_name = "node:22"
            else:
                image_name = "python:3.12"

        log.write(f"Using image: {image_name}")

        # Run container
        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d", "--network", "host",
                    "--name", container_name,
                    "-v", f"{project_path}:/workspace",
                    "-w", "/workspace",
                    image_name, "sleep", "infinity",
                ],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                log.write(f"[red]Failed to start container:[/red]")
                log.write(f"  [red]{result.stderr.strip()}[/red]")
                save_alpha_build(
                    project_id=project_id, container_name=container_name,
                    image=image_name, status="failed",
                    build_log=result.stderr,
                )
                self.call_from_thread(self._refresh_builds_tab)
                return

            container_id = result.stdout.strip()[:12]
            log.write(f"[green]Container started: {container_id}[/green]")

            save_alpha_build(
                project_id=project_id, container_id=container_id,
                container_name=container_name, image=image_name,
                status="running",
            )

            log.write(f"[bold green]Alpha build running: {container_name}[/bold green]")
            log.write(f"[dim]Shell in: docker exec -it {container_name} bash[/dim]")
            self.call_from_thread(self._refresh_builds_tab)
            self.call_from_thread(self.notify, f"Build launched: {container_name}")

        except subprocess.TimeoutExpired:
            log.write("[red]Container start timed out (60s)[/red]")
            self.call_from_thread(self.notify, "Container start timed out", severity="error")
        except Exception as e:
            log.write(f"[red]Error: {e}[/red]")
            self.call_from_thread(self.notify, f"Build error: {e}", severity="error")

    @work(thread=True)
    def _do_stop_build(self):
        """Stop the selected alpha build container."""
        if not self._selected_build_id:
            self.call_from_thread(self.notify, "Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            """SELECT ab.*, p.name as project_name
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.id = ?""",
            (self._selected_build_id,),
        ).fetchone()

        if not build:
            conn.close()
            self.call_from_thread(self.notify, "Build not found", severity="error")
            return

        log = self.query_one("#builds-log", RichLog)
        container_name = build["container_name"]
        log.write(f"[bold yellow]Stopping {container_name}...[/bold yellow]")

        result = subprocess.run(
            ["docker", "stop", container_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            conn.execute(
                "UPDATE alpha_builds SET status='stopped', stopped_at=datetime('now') WHERE id=?",
                (self._selected_build_id,),
            )
            conn.commit()
            log.write(f"[bold green]Stopped {container_name}[/bold green]")
        else:
            log.write(f"[red]Stop failed: {result.stderr.strip()}[/red]")

        conn.close()
        self.call_from_thread(self._refresh_builds_tab)

    @work(thread=True)
    def _do_rebuild(self):
        """Stop, remove, and relaunch the selected build."""
        if not self._selected_build_id:
            self.call_from_thread(self.notify, "Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            "SELECT * FROM alpha_builds WHERE id = ?", (self._selected_build_id,)
        ).fetchone()
        conn.close()

        if not build:
            self.call_from_thread(self.notify, "Build not found", severity="error")
            return

        log = self.query_one("#builds-log", RichLog)
        container_name = build["container_name"]

        log.write(f"[bold yellow]Rebuilding {container_name}...[/bold yellow]")

        # Stop and remove
        subprocess.run(["docker", "stop", container_name], capture_output=True, timeout=30)
        subprocess.run(["docker", "rm", container_name], capture_output=True, timeout=10)

        # Remove old DB entry
        conn = get_db()
        conn.execute("DELETE FROM alpha_builds WHERE id = ?", (self._selected_build_id,))
        conn.commit()
        conn.close()

        # Relaunch
        self._selected_build_id = None
        self.call_from_thread(self._do_launch_build, build["project_id"])

    def _do_shell_build(self):
        """Open an interactive shell into the selected build container."""
        if not self._selected_build_id:
            self.notify("Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            "SELECT container_name FROM alpha_builds WHERE id = ?",
            (self._selected_build_id,),
        ).fetchone()
        conn.close()

        if not build:
            self.notify("Build not found", severity="error")
            return

        container_name = build["container_name"]
        session_name = f"shell-{container_name}"

        # Open in a tmux session
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       capture_output=True)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name,
             "docker", "exec", "-it", container_name, "bash"],
            capture_output=True,
        )
        self.notify(f"Shell opened: tmux attach -t {session_name}")

        log = self.query_one("#builds-log", RichLog)
        log.write(f"[green]Shell session: {session_name}[/green]")
        log.write(f"[dim]Run: tmux attach -t {session_name}[/dim]")

    @work(thread=True)
    def _do_build_test(self):
        """Run tests inside the selected build container."""
        if not self._selected_build_id:
            self.call_from_thread(self.notify, "Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            """SELECT ab.*, p.name as project_name, p.path as project_path, p.stack
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.id = ?""",
            (self._selected_build_id,),
        ).fetchone()
        conn.close()

        if not build or build["status"] != "running":
            self.call_from_thread(self.notify, "Build must be running to test", severity="warning")
            return

        log = self.query_one("#builds-log", RichLog)
        container = build["container_name"]
        stack = build["stack"] or ""

        # Auto-detect test command
        if "Python" in stack:
            test_cmd = "pytest -v"
        elif any(s in stack for s in ("Node", "React", "Next", "Electron")):
            test_cmd = "npm test"
        else:
            test_cmd = "pytest -v"

        log.write(f"[bold blue]Running tests: {test_cmd}[/bold blue]")

        try:
            result = subprocess.run(
                ["docker", "exec", container, "bash", "-c", test_cmd],
                capture_output=True, text=True, timeout=120,
            )
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    log.write(line)
            if result.stderr.strip():
                for line in result.stderr.strip().split("\n")[-10:]:
                    log.write(f"[red]{line}[/red]")
            if result.returncode == 0:
                log.write("[bold green]Tests passed[/bold green]")
            else:
                log.write(f"[bold red]Tests failed (exit {result.returncode})[/bold red]")
        except subprocess.TimeoutExpired:
            log.write("[red]Tests timed out (120s)[/red]")
        except Exception as e:
            log.write(f"[red]Error: {e}[/red]")

    @work(thread=True)
    def _do_build_install(self):
        """Install dependencies inside the selected build container."""
        if not self._selected_build_id:
            self.call_from_thread(self.notify, "Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            """SELECT ab.*, p.name as project_name, p.stack
               FROM alpha_builds ab
               JOIN projects p ON p.id = ab.project_id
               WHERE ab.id = ?""",
            (self._selected_build_id,),
        ).fetchone()
        conn.close()

        if not build or build["status"] != "running":
            self.call_from_thread(self.notify, "Build must be running to install", severity="warning")
            return

        log = self.query_one("#builds-log", RichLog)
        container = build["container_name"]
        stack = build["stack"] or ""

        # Auto-detect install command
        if "Python" in stack:
            install_cmd = "pip install -r requirements.txt"
        elif any(s in stack for s in ("Node", "React", "Next", "Electron")):
            install_cmd = "npm install"
        else:
            install_cmd = "pip install -r requirements.txt"

        log.write(f"[bold blue]Installing deps: {install_cmd}[/bold blue]")

        try:
            result = subprocess.run(
                ["docker", "exec", "-w", "/workspace", container, "bash", "-c", install_cmd],
                capture_output=True, text=True, timeout=180,
            )
            for line in result.stdout.strip().split("\n")[-15:]:
                if line.strip():
                    log.write(line)
            if result.returncode == 0:
                log.write("[bold green]Dependencies installed[/bold green]")
            else:
                log.write(f"[red]Install failed: {result.stderr.strip()[-200:]}[/red]")
        except subprocess.TimeoutExpired:
            log.write("[red]Install timed out (180s)[/red]")
        except Exception as e:
            log.write(f"[red]Error: {e}[/red]")

    @work(thread=True)
    def _do_build_logs(self):
        """Stream recent Docker logs for the selected build."""
        if not self._selected_build_id:
            self.call_from_thread(self.notify, "Select a build first", severity="warning")
            return

        conn = get_db()
        build = conn.execute(
            "SELECT container_name FROM alpha_builds WHERE id = ?",
            (self._selected_build_id,),
        ).fetchone()
        conn.close()

        if not build:
            self.call_from_thread(self.notify, "Build not found", severity="error")
            return

        log = self.query_one("#builds-log", RichLog)
        log.clear()
        container = build["container_name"]
        log.write(f"[bold blue]Logs for {container}:[/bold blue]")

        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", "50", container],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stdout + result.stderr
            if output.strip():
                for line in output.strip().split("\n"):
                    log.write(line)
            else:
                log.write("[dim]No logs yet[/dim]")
        except Exception as e:
            log.write(f"[red]Error reading logs: {e}[/red]")

    # ── Editor Tab ──────────────────────────────────────────────────

    def _init_editor_tab(self):
        """Initialize editor tab: load saved Claude session if any."""
        session = self._load_claude_session()
        label = self.query_one("#session-label", Static)
        if session:
            session_id = session.get("session_id")
            self._claude_session_id = session_id
            if session_id:
                label.update(f"Session: {session_id[:12]}... (saved)")
            else:
                label.update("No session")
        else:
            label.update("No session")
        self._refresh_git_status()

    def _open_editor_project(self, project_id):
        """Switch the Editor tab to a specific project."""
        conn = get_db()
        proj = conn.execute(
            "SELECT name, path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.close()

        if not proj:
            self.notify("Project not found", severity="error")
            return

        native_path = _to_native_path(proj["path"])
        if not os.path.isdir(native_path):
            self.notify(f"Project path not found: {native_path}", severity="error")
            return

        self._editor_project_name = proj["name"]
        self._editor_project_path = native_path

        # Reload file tree with project root
        tree = self.query_one("#editor-tree", WorkbenchDirectoryTree)
        tree.path = native_path

        # Clear editor
        textarea = self.query_one("#editor-textarea", TextArea)
        textarea.load_text("")
        label = self.query_one("#editor-file-label", Static)
        label.update(f"[bold]{proj['name']}[/bold] — select a file")
        self._editor_current_file = None
        self._editor_modified = False

        # Refresh git for this project
        self._refresh_git_status()

        # Auto-create session if needed
        if not self._claude_session_id:
            self._do_new_claude_session()

        # Show project in chat
        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.write(f"\n[bold green]Switched to project: {proj['name']}[/bold green]")
        chat_log.write(f"[dim]Path: {native_path}[/dim]")

        self.notify(f"Opened {proj['name']}")

    # ── Git Integration ──────────────────────────────────────────────

    def _refresh_git_status(self):
        """Update the git status label in the Editor tab."""
        git_cwd = self._editor_project_path or self._workbench_path
        label = self.query_one("#git-status-label", Static)
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True,
                cwd=git_cwd, timeout=10,
            )
            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            if not lines:
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True,
                    cwd=git_cwd, timeout=5,
                ).stdout.strip()
                label.update(f"git: {branch} — clean")
            else:
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True,
                    cwd=git_cwd, timeout=5,
                ).stdout.strip()
                label.update(f"git: {branch} — {len(lines)} changed file{'s' if len(lines) != 1 else ''}")
        except Exception as e:
            label.update(f"git: error ({e})")

    @work(thread=True)
    def _do_git_commit_push(self):
        """Stage all changes, commit with timestamp, push to current branch."""
        git_cwd = self._editor_project_path or self._workbench_path
        label = self.query_one("#git-status-label", Static)
        chat = self.query_one("#claude-chat-log", RichLog)

        # Check for changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=10,
        )
        changed = [l for l in result.stdout.strip().split("\n") if l.strip()]
        if not changed:
            self.call_from_thread(self.notify, "Nothing to commit — working tree clean", severity="warning")
            return

        # Detect current branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=5,
        ).stdout.strip() or "main"

        label.update("git: staging...")
        chat.write(f"[bold cyan]Git:[/] Staging {len(changed)} file(s)...")

        # Stage all
        subprocess.run(
            ["git", "add", "-A"],
            cwd=git_cwd, timeout=30,
        )

        # Commit
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"Admin TUI sync — {timestamp}"
        label.update("git: committing...")
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=30,
        )
        if result.returncode != 0:
            chat.write(f"[red]Git commit failed:[/] {result.stderr.strip()}")
            self._refresh_git_status()
            return

        chat.write(f"[bold cyan]Git:[/] Committed: {msg}")

        # Push
        label.update("git: pushing...")
        result = subprocess.run(
            ["git", "push", "origin", branch],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=60,
        )
        if result.returncode != 0:
            chat.write(f"[red]Git push failed:[/] {result.stderr.strip()}")
        else:
            chat.write(f"[bold green]Git:[/] Pushed to origin/{branch}")

        self.call_from_thread(self._refresh_git_status)

    @work(thread=True)
    def _do_git_pull(self):
        """Pull latest from current branch."""
        git_cwd = self._editor_project_path or self._workbench_path
        label = self.query_one("#git-status-label", Static)
        chat = self.query_one("#claude-chat-log", RichLog)

        # Detect current branch
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=5,
        ).stdout.strip() or "main"

        label.update("git: pulling...")
        chat.write(f"[bold cyan]Git:[/] Pulling from origin/{branch}...")

        result = subprocess.run(
            ["git", "pull", "origin", branch],
            capture_output=True, text=True,
            cwd=git_cwd, timeout=60,
        )
        if result.returncode != 0:
            chat.write(f"[red]Git pull failed:[/] {result.stderr.strip()}")
        else:
            output = result.stdout.strip()
            if "Already up to date" in output:
                chat.write("[bold cyan]Git:[/] Already up to date")
            else:
                chat.write(f"[bold green]Git:[/] {output}")
                # Reload current file if it was updated
                if self._editor_current_file:
                    self.call_from_thread(self._reload_current_file)

        self.call_from_thread(self._refresh_git_status)

    def _get_language_for_file(self, filepath):
        """Map file extension to TextArea language name."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "javascript",
            ".tsx": "javascript",
            ".jsx": "javascript",
            ".json": "json",
            ".md": "markdown",
            ".css": "css",
            ".html": "html",
            ".sql": "sql",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
        }
        ext = os.path.splitext(filepath)[1].lower()
        return ext_map.get(ext)

    def _open_file_in_editor(self, filepath):
        """Read a file and display it in the TextArea."""
        textarea = self.query_one("#editor-textarea", TextArea)
        label = self.query_one("#editor-file-label", Static)

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            self.notify(f"Cannot open: {e}", severity="error")
            return

        textarea.load_text(content)
        lang = self._get_language_for_file(filepath)
        if lang:
            textarea.language = lang
        else:
            textarea.language = None

        self._editor_current_file = filepath
        self._editor_modified = False

        # Show relative path from workbench root
        try:
            rel = os.path.relpath(filepath, self._workbench_path)
        except ValueError:
            rel = filepath
        label.update(rel)

    def _save_current_file(self):
        """Write TextArea content back to disk."""
        if not self._editor_current_file:
            self.notify("No file open", severity="warning")
            return

        textarea = self.query_one("#editor-textarea", TextArea)
        content = textarea.text

        try:
            with open(self._editor_current_file, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            self._editor_modified = False
            self.notify(f"Saved {os.path.basename(self._editor_current_file)}")
            self._refresh_git_status()
        except OSError as e:
            self.notify(f"Save failed: {e}", severity="error")

    def _reload_current_file(self):
        """Re-read current file from disk, discarding unsaved edits."""
        if not self._editor_current_file:
            self.notify("No file open", severity="warning")
            return
        self._open_file_in_editor(self._editor_current_file)
        self.notify("Reloaded from disk")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        """Handle file selection in the editor tree."""
        filepath = str(event.path)
        # Auto-save previous file if modified
        if self._editor_modified and self._editor_current_file:
            self._save_current_file()
        self._open_file_in_editor(filepath)

    # ── Claude Session Management ──────────────────────────────────

    def _load_claude_session(self):
        """Read saved session ID from disk."""
        try:
            with open(self._session_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _save_claude_session(self, session_id):
        """Write session ID to disk."""
        data = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
        }
        try:
            with open(self._session_file, "w") as f:
                json.dump(data, f)
        except OSError as e:
            self.notify(f"Could not save session: {e}", severity="error")

    def _clear_claude_session(self):
        """Remove any saved OpenCode session."""
        try:
            os.remove(self._session_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            self.notify(f"Could not clear session: {e}", severity="error")

    def _get_fossil_briefs(self):
        """Fetch one-line summaries for all projects from the latest fossils."""
        try:
            conn = get_db()
            rows = conn.execute(
                """SELECT p.name, p.stack, f.summary, f.version, p.last_indexed
                   FROM projects p
                   LEFT JOIN fossils f ON f.id = (
                       SELECT id FROM fossils
                       WHERE project_id = p.id
                       ORDER BY created_at DESC LIMIT 1
                   )
                   WHERE p.status = 'active'
                   ORDER BY p.name"""
            ).fetchall()
            conn.close()
            lines = []
            for r in rows:
                summary = (r["summary"] or "no fossil yet")[:120]
                indexed = r["last_indexed"] or "never"
                lines.append(f"  {r['name']} ({r['stack'] or '?'}): {summary} [indexed: {indexed}]")
            return "\n".join(lines)
        except Exception:
            return "  (could not load project briefs)"

    def _do_new_claude_session(self):
        """Clear the current OpenCode session and prepare a fresh one."""
        self._claude_session_id = None
        self._clear_claude_session()

        label = self.query_one("#session-label", Static)
        label.update("Session: new [green]ready[/green]")

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.clear()
        chat_log.write("[bold green]OpenCode session ready[/bold green]")
        chat_log.write("")
        chat_log.write("[bold]OpenCode is your on-demand developer. Tell it what to build, fix, or change.[/bold]")
        chat_log.write("[dim]Tools: Read, Edit, Write, Bash, Glob, Grep + Custodian MCP (fossils, symbols, insights)[/dim]")
        chat_log.write("[dim]Open a file in the tree -> OpenCode sees it as context. Editor auto-reloads after edits.[/dim]")
        chat_log.write("[dim]Session persists across restarts. Use 'Resume' to continue later.[/dim]")
        chat_log.write("")

    def _do_resume_claude_session(self):
        """Resume a previously saved Claude session."""
        session = self._load_claude_session()
        if not session:
            self.notify("No saved session found", severity="warning")
            return

        session_id = session.get("session_id")
        if not session_id:
            self.notify("Saved session is missing an id", severity="error")
            return

        self._claude_session_id = session_id
        label = self.query_one("#session-label", Static)
        label.update(f"Session: {session_id[:12]}... (resumed)")

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.write(
            f"[bold blue]Resumed OpenCode session: {self._claude_session_id}[/bold blue]\n"
            f"Created: {session.get('created_at', 'unknown')}"
        )
        self.notify("Session resumed")

    # ── Claude Chat ────────────────────────────────────────────────

    def _detect_project_for_file(self, filepath):
        """Figure out which registered project a file belongs to."""
        try:
            conn = get_db()
            projects = conn.execute(
                "SELECT name, path FROM projects WHERE status = 'active'"
            ).fetchall()
            conn.close()
            abs_file = os.path.normpath(os.path.abspath(filepath))
            for p in projects:
                proj_path = os.path.normpath(os.path.abspath(p["path"]))
                if abs_file.startswith(proj_path):
                    return p["name"]
        except Exception:
            pass
        return None

    def _build_claude_prompt(self, message):
        """Build prompt with file context and project detection.

        Tells Claude which file is open, which project it belongs to (so it can
        query the right fossil), and includes the file content for immediate context.
        """
        parts = []
        # If a project is explicitly selected, always include project context
        if self._editor_project_name:
            parts.append(
                f"I am working on the '{self._editor_project_name}' project "
                f"(path: {self._editor_project_path}). "
                f"Use get_project_fossil('{self._editor_project_name}') for architecture context."
            )
        if self._editor_current_file:
            try:
                with open(self._editor_current_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[:2000]
                content = "".join(lines)
                base_path = self._editor_project_path or self._workbench_path
                rel = os.path.relpath(self._editor_current_file, base_path)

                project = self._editor_project_name or self._detect_project_for_file(self._editor_current_file)
                header = f"I am currently viewing the file {rel} in the NAI Workbench editor."
                if project and not self._editor_project_name:
                    header += (
                        f"\nThis file belongs to the '{project}' project. "
                        f"Use get_project_fossil('{project}') for full architecture context."
                    )

                parts.append(f"{header}\n\n<file path=\"{rel}\">\n{content}\n</file>\n")
            except OSError:
                pass
        parts.append(message)
        return "\n".join(parts)

    def _do_send_claude_message(self):
        """Send the chat input to OpenCode."""
        chat_input = self.query_one("#chat-input", Input)
        message = chat_input.value.strip()
        if not message:
            return

        if self._claude_running:
            self.notify("OpenCode is still running - wait or click Stop", severity="warning")
            return

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.write(f"\n[bold cyan]You:[/bold cyan] {message}")
        chat_input.value = ""

        # Show working indicator
        session_label = self.query_one("#session-label", Static)
        session_name = f"{self._claude_session_id[:12]}..." if self._claude_session_id else "new"
        session_label.update(f"Session: {session_name} [bold yellow]working...[/bold yellow]")

        prompt = self._build_claude_prompt(message)
        self._run_claude_query(prompt)

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in chat input."""
        if event.input.id == "chat-input":
            self._do_send_claude_message()

    def _extract_text_from_event(self, event_data):
        """Extract displayable text from an OpenCode JSON event.

        Also tracks which files OpenCode edits/writes so we can auto-reload.
        """
        event_type = event_data.get("type", "")
        session_id = event_data.get("sessionID")
        part = event_data.get("part") or {}

        if session_id and session_id != self._claude_session_id:
            self._claude_session_id = session_id
            self._save_claude_session(session_id)
            try:
                label = self.query_one("#session-label", Static)
                self.call_from_thread(
                    label.update,
                    f"Session: {session_id[:12]}... [green]ready[/green]",
                )
            except Exception:
                pass

        if event_type == "text":
            return part.get("text", "")

        if event_type == "tool_use":
            tool = part.get("tool", "unknown")
            state = part.get("state") or {}
            tool_input = state.get("input") or {}

            fp = (
                tool_input.get("filePath")
                or tool_input.get("path")
                or tool_input.get("source")
                or tool_input.get("destination")
                or ""
            )
            if fp and any(key in tool for key in ("edit", "write", "patch", "move", "delete")):
                self._claude_edited_files.add(fp)

            if any(key in tool for key in ("edit", "write", "patch", "move", "delete")):
                color = "bold red"
            elif tool in ("read", "glob", "grep"):
                color = "bold blue"
            elif tool == "bash":
                color = "bold yellow"
            elif tool.startswith("mcp"):
                color = "bold cyan"
            else:
                color = "bold magenta"

            if fp:
                try:
                    fp = os.path.relpath(fp, self._workbench_path)
                except ValueError:
                    pass
                return f"\n[{color}]>>> {tool}[/{color}] {fp}\n"

            cmd = tool_input.get("command", "")
            if cmd:
                return f"\n[{color}]>>> {tool}[/{color}] `{cmd[:100]}`\n"

            params = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:3] if v)
            if params:
                return f"\n[{color}]>>> {tool}[/{color}] ({params})\n"
            return f"\n[{color}]>>> {tool}[/{color}]\n"

        if event_type == "step_finish":
            tokens = part.get("tokens") or {}
            meta = []
            if part.get("cost") is not None:
                meta.append(f"${part['cost']:.4f}")
            if tokens.get("total") is not None:
                meta.append(f"{tokens['total']} tok")
            if meta and part.get("reason") == "stop":
                return f"[dim]({', '.join(meta)})[/dim]"
            return ""

        if event_type == "error":
            error = event_data.get("error") or {}
            message = error.get("data", {}).get("message") or error.get("message") or "Unknown error"
            return f"[bold red]ERROR:[/bold red] [red]{message}[/red]"

        return ""

    @work(thread=True)
    def _run_claude_query(self, prompt):
        """Spawn OpenCode and stream JSON response events to the chat log."""
        chat_log = self.query_one("#claude-chat-log", RichLog)
        self._claude_running = True
        self._claude_edited_files = set()

        env = os.environ.copy()
        env.setdefault("OPENCODE_DISABLE_UPDATE_CHECK", "1")

        # Build startup prompt with fossil briefs so OpenCode knows the project landscape
        briefs = self._get_fossil_briefs()
        system_ctx = EDITOR_SYSTEM_PROMPT + f"\nRegistered projects:\n{briefs}\n"

        opencode_cwd = self._editor_project_path or self._workbench_path
        cmd = [
            OPENCODE_BIN,
            "run",
            "--format", "json",
            "--dir", opencode_cwd,
            "--model", OPENCODE_MODEL,
        ]
        if self._claude_session_id:
            cmd.extend(["--session", self._claude_session_id])
        cmd.append(f"{system_ctx}\n\n{prompt}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=opencode_cwd,
            )
            self._claude_process = proc
            if proc.stdout is None or proc.stderr is None:
                raise RuntimeError("OpenCode process streams were not available")

            chat_log.write("[bold yellow]OpenCode:[/bold yellow]")

            # Stream NDJSON response
            buffer = ""
            for raw_line in iter(proc.stdout.readline, ""):
                if not self._claude_running:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event_data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                text = self._extract_text_from_event(event_data)
                if text:
                    buffer += text
                    # Write complete lines as they arrive
                    while "\n" in buffer:
                        line_text, buffer = buffer.split("\n", 1)
                        if line_text.strip():
                            chat_log.write(line_text)

            # Flush remaining buffer
            if buffer.strip():
                chat_log.write(buffer)

            proc.wait()

            if proc.returncode != 0:
                stderr = proc.stderr.read()
                if stderr.strip():
                    chat_log.write(f"[red]{stderr.strip()}[/red]")

            # Show summary of edited files
            if self._claude_edited_files:
                chat_log.write(
                    f"\n[bold green]Files modified ({len(self._claude_edited_files)}):[/bold green]"
                )
                for fp in sorted(self._claude_edited_files):
                    try:
                        rel = os.path.relpath(fp, self._workbench_path)
                    except ValueError:
                        rel = fp
                    chat_log.write(f"  [green]{rel}[/green]")

                # Auto-reload editor if the open file was edited
                if self._editor_current_file:
                    cur = os.path.normpath(os.path.abspath(self._editor_current_file))
                    for fp in self._claude_edited_files:
                        edited = os.path.normpath(os.path.abspath(fp))
                        if edited == cur:
                            self.call_from_thread(self._reload_current_file)
                            chat_log.write("[bold green]Editor auto-reloaded.[/bold green]")
                            break

                # Refresh git status since files changed
                self.call_from_thread(self._refresh_git_status)

        except FileNotFoundError:
            chat_log.write(f"[red]OpenCode CLI not found at {OPENCODE_BIN}[/red]")
        except Exception as e:
            chat_log.write(f"[red]Error: {type(e).__name__}: {e}[/red]")
        finally:
            self._claude_running = False
            self._claude_process = None
            # Clear "working" indicator
            try:
                label = self.query_one("#session-label", Static)
                session_name = f"{self._claude_session_id[:12]}..." if self._claude_session_id else "new"
                self.call_from_thread(
                    label.update,
                    f"Session: {session_name} [green]ready[/green]"
                )
            except Exception:
                pass

    def _do_stop_claude(self):
        """Terminate the running OpenCode process."""
        if self._claude_process and self._claude_running:
            self._claude_running = False
            try:
                self._claude_process.terminate()
            except OSError:
                pass
            chat_log = self.query_one("#claude-chat-log", RichLog)
            chat_log.write("[yellow]Stopped.[/yellow]")
            self.notify("OpenCode stopped")
        else:
            self.notify("Nothing running", severity="warning")

    # ── Devices Tab ──────────────────────────────────────────────────

    def _refresh_devices_tab(self):
        """Refresh the Devices tab — populate DataTable."""
        try:
            table = self.query_one("#devices-table", DataTable)
            table.clear(columns=True)
            table.add_columns("ID", "Name", "Hostname", "Tailscale IP", "Fingerprint", "Paired", "Status")
            table.cursor_type = "row"

            conn = get_db()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS devices "
                "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, hostname TEXT, "
                "tailscale_ip TEXT, ssh_pubkey TEXT, ssh_fingerprint TEXT, "
                "paired_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT, "
                "status TEXT DEFAULT 'paired')"
            )
            rows = conn.execute(
                "SELECT id, name, hostname, tailscale_ip, ssh_fingerprint, paired_at, status "
                "FROM devices ORDER BY paired_at DESC"
            ).fetchall()
            conn.close()

            for r in rows:
                fp = (r["ssh_fingerprint"] or "")[:20]
                paired = (r["paired_at"] or "")[:16]
                status = r["status"] or "paired"
                table.add_row(
                    str(r["id"]),
                    r["name"] or "",
                    r["hostname"] or "",
                    r["tailscale_ip"] or "",
                    fp + "..." if len(r["ssh_fingerprint"] or "") > 20 else fp,
                    paired,
                    status,
                )
        except Exception:
            pass

    def _do_generate_pair_code(self):
        """Generate a new pairing code and display it."""
        try:
            conn = get_db()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pairing_codes "
                "(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, "
                "used_by_device_id INTEGER, status TEXT DEFAULT 'pending')"
            )
            # Expire old codes
            conn.execute(
                "UPDATE pairing_codes SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < datetime('now')"
            )
            # Generate code
            import secrets as _secrets
            import string as _string
            chars = _string.ascii_uppercase + _string.digits
            chars = chars.replace("O", "").replace("0", "").replace("I", "").replace("1", "").replace("L", "")
            suffix = "".join(_secrets.choice(chars) for _ in range(4))
            code = f"NAI-{suffix}"
            from datetime import datetime as _dt, timedelta as _td
            expires = (_dt.utcnow() + _td(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO pairing_codes (code, expires_at) VALUES (?, ?)",
                (code, expires),
            )
            conn.commit()
            conn.close()

            display = self.query_one("#pairing-code-display", Static)
            display.update(
                f"[bold green]Pairing Code: [white on blue] {code} [/white on blue][/bold green]\n"
                f"[dim]Expires in 10 minutes. On the new device, run:[/dim]\n"
                f"[bold]curl -sL http://PC_IP:7777/setup | bash[/bold]\n"
                f"[dim]or: bash bin/setup-device[/dim]"
            )
            log = self.query_one("#devices-log", RichLog)
            log.write(f"[green]Generated pairing code:[/green] {code} (expires {expires})")
            self.notify(f"Pairing code: {code}", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _do_remove_device(self):
        """Remove/revoke selected device and its SSH key."""
        table = self.query_one("#devices-table", DataTable)
        row_idx = table.cursor_row
        if row_idx is None:
            self.notify("Select a device first", severity="warning")
            return
        try:
            row_data = table.get_row_at(row_idx)
            device_id = int(row_data[0])
        except Exception:
            self.notify("Select a device first", severity="warning")
            return

        try:
            conn = get_db()
            device = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            if not device:
                conn.close()
                self.notify("Device not found", severity="error")
                return

            # Mark as revoked
            conn.execute(
                "UPDATE devices SET status = 'revoked' WHERE id = ?", (device_id,)
            )
            conn.commit()
            conn.close()

            # Remove pubkey from authorized_keys
            pubkey = device["ssh_pubkey"]
            ak_path = os.path.expanduser("~/.ssh/authorized_keys")
            if pubkey and os.path.isfile(ak_path):
                with open(ak_path, "r") as f:
                    lines = f.readlines()
                # Filter out lines containing this key (match the key portion)
                key_parts = pubkey.strip().split()
                key_data = key_parts[1] if len(key_parts) > 1 else pubkey.strip()
                new_lines = [l for l in lines if key_data not in l]
                if len(new_lines) != len(lines):
                    with open(ak_path, "w") as f:
                        f.writelines(new_lines)

            log = self.query_one("#devices-log", RichLog)
            log.write(f"[red]Revoked device:[/red] {device['name']} (SSH key removed)")
            self.notify(f"Device '{device['name']}' revoked")
            self._refresh_devices_tab()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    # ── Ticker Tab ────────────────────────────────────────────────────

    def _refresh_ticker_tab(self):
        """Refresh the Ticker tab — segments table, settings inputs, preview."""
        try:
            table = self.query_one("#ticker-segments-table", DataTable)
            table.clear(columns=True)
            table.add_columns("Order", "Key", "Label", "Enabled")
            table.cursor_type = "row"

            conn = get_db()
            # Ensure table has new columns
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ticker_config)").fetchall()]
            if "display_order" not in cols:
                conn.execute("ALTER TABLE ticker_config ADD COLUMN display_order INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE ticker_config ADD COLUMN label TEXT")
                conn.execute("ALTER TABLE ticker_config ADD COLUMN format TEXT")
                conn.commit()

            rows = conn.execute(
                "SELECT key, enabled, display_order, label FROM ticker_config ORDER BY display_order, key"
            ).fetchall()

            # Load settings
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ticker_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            settings_rows = conn.execute("SELECT key, value FROM ticker_settings").fetchall()
            conn.close()

            settings = {}
            for r in settings_rows:
                settings[r["key"]] = r["value"]

            for r in rows:
                table.add_row(
                    str(r["display_order"] or 0),
                    r["key"],
                    r["label"] or r["key"].title(),
                    "Yes" if r["enabled"] else "No",
                )

            # Populate settings inputs
            try:
                self.query_one("#ticker-speed", Input).value = settings.get("scroll_speed", "50")
                self.query_one("#ticker-opacity", Input).value = settings.get("opacity", "85")
                self.query_one("#ticker-height", Input).value = settings.get("bar_height", "28")
                self.query_one("#ticker-poll", Input).value = settings.get("poll_interval", "3")
                pos = settings.get("position", "top")
                self.query_one("#ticker-position", Select).value = pos
            except Exception:
                pass

            # Update preview
            enabled = [r["label"] or r["key"].title() for r in rows if r["enabled"]]
            preview_text = " | ".join(enabled) if enabled else "(all segments disabled)"
            try:
                self.query_one("#ticker-preview", Static).update(
                    f"[dim]Enabled segments:[/dim] {preview_text}"
                )
            except Exception:
                pass
        except Exception:
            pass

    def _move_ticker_segment(self, direction: int):
        """Move the selected segment up or down in display order."""
        table = self.query_one("#ticker-segments-table", DataTable)
        row_idx = table.cursor_row
        if row_idx is None:
            self.notify("Select a segment first", severity="warning")
            return
        try:
            row_data = table.get_row_at(row_idx)
            key = row_data[1]
        except Exception:
            self.notify("Select a segment first", severity="warning")
            return

        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT key, display_order FROM ticker_config ORDER BY display_order, key"
            ).fetchall()
            keys = [r["key"] for r in rows]
            idx = keys.index(key) if key in keys else -1
            new_idx = idx + direction
            if idx < 0 or new_idx < 0 or new_idx >= len(keys):
                conn.close()
                return
            # Swap display_order values
            other_key = keys[new_idx]
            conn.execute(
                "UPDATE ticker_config SET display_order = ? WHERE key = ?",
                (new_idx, key),
            )
            conn.execute(
                "UPDATE ticker_config SET display_order = ? WHERE key = ?",
                (idx, other_key),
            )
            conn.commit()
            conn.close()
            self._refresh_ticker_tab()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _toggle_ticker_segment(self):
        """Toggle the enabled state of the selected segment."""
        table = self.query_one("#ticker-segments-table", DataTable)
        row_idx = table.cursor_row
        if row_idx is None:
            self.notify("Select a segment first", severity="warning")
            return
        try:
            row_data = table.get_row_at(row_idx)
            key = row_data[1]
        except Exception:
            self.notify("Select a segment first", severity="warning")
            return

        try:
            conn = get_db()
            row = conn.execute(
                "SELECT enabled FROM ticker_config WHERE key = ?", (key,)
            ).fetchone()
            if row:
                new_val = 0 if row["enabled"] else 1
                conn.execute(
                    "UPDATE ticker_config SET enabled = ? WHERE key = ?", (new_val, key)
                )
                conn.commit()
            conn.close()
            self._refresh_ticker_tab()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _save_ticker_settings(self):
        """Save overlay settings from the Ticker tab inputs to DB."""
        try:
            settings = {
                "scroll_speed": self.query_one("#ticker-speed", Input).value,
                "opacity": self.query_one("#ticker-opacity", Input).value,
                "bar_height": self.query_one("#ticker-height", Input).value,
                "poll_interval": self.query_one("#ticker-poll", Input).value,
                "position": self.query_one("#ticker-position", Select).value or "top",
            }
            conn = get_db()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ticker_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for k, v in settings.items():
                conn.execute(
                    "INSERT OR REPLACE INTO ticker_settings (key, value) VALUES (?, ?)",
                    (k, str(v)),
                )
            conn.commit()
            conn.close()
            self.notify("Ticker settings saved")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _launch_ticker_overlay(self):
        """Launch the ticker overlay process."""
        if self._ticker_overlay_proc and self._ticker_overlay_proc.poll() is None:
            self.notify("Overlay already running", severity="warning")
            return
        try:
            overlay_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "ticker_overlay.py"
            )
            is_wsl = os.path.exists("/proc/version") and "microsoft" in open("/proc/version").read().lower()
            if is_wsl:
                # Convert Linux path to Windows path for pythonw.exe
                win_path = subprocess.check_output(
                    ["wslpath", "-w", overlay_path], text=True
                ).strip()
                self._ticker_overlay_proc = subprocess.Popen(
                    ["pythonw.exe", win_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                for exe in ["pythonw.exe", "python3", "python"]:
                    try:
                        self._ticker_overlay_proc = subprocess.Popen(
                            [exe, overlay_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        break
                    except FileNotFoundError:
                        continue
                else:
                    self.notify("Could not find Python executable", severity="error")
                    return
            self.notify(f"Overlay launched (PID {self._ticker_overlay_proc.pid})")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def _stop_ticker_overlay(self):
        """Stop the ticker overlay process."""
        if self._ticker_overlay_proc and self._ticker_overlay_proc.poll() is None:
            self._ticker_overlay_proc.terminate()
            self._ticker_overlay_proc = None
        # Also kill any orphaned Windows pythonw instances running the overlay
        is_wsl = os.path.exists("/proc/version") and "microsoft" in open("/proc/version").read().lower()
        if is_wsl:
            try:
                subprocess.run(
                    ["powershell.exe", "-Command",
                     "Get-Process pythonw -ErrorAction SilentlyContinue | "
                     "Where-Object { $_.CommandLine -like '*ticker_overlay*' } | "
                     "Stop-Process -Force"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        else:
            try:
                subprocess.run(["pkill", "-f", "ticker_overlay.py"], capture_output=True, timeout=3)
            except Exception:
                pass
        self.notify("Overlay stopped")

    # ── Actions ───────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        """Refresh all tabs."""
        self._load_projects()
        self._refresh_projects_tab()
        self._refresh_custodian_tab()
        self._refresh_fossils_tab()
        self._refresh_detective_tab()
        self._refresh_status_tab()
        self._refresh_agents_tab()
        self._refresh_builds_tab()
        self._refresh_devices_tab()
        self._refresh_ticker_tab()
        self.notify("Refreshed")

    def action_focus_tab(self, tab_name: str) -> None:
        """Switch to a specific tab."""
        tabbed = self.query_one(TabbedContent)
        tabbed.active = f"tab-{tab_name}"


if __name__ == "__main__":
    app = CustodianAdmin()
    app.run()
