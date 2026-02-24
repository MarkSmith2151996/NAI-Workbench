#!/usr/bin/env python3
"""NAI Workbench — ADMIN 01: Custodian Administration TUI.

6 tabs:
- Projects: Import from GitHub, register local, manage projects
- Custodian: Index projects (trigger Sonnet)
- Fossils: Browse fossil history, view details, compare
- Detective: Run analysis, view insights, refine prompts
- Status: System overview (DB stats, project status)
- Editor: File browser + code editor + persistent Claude Code chat
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
PROJECTS_DIR = os.path.expanduser("~/projects")

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

# System prompt injected into every Editor Claude session so it knows about
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
        self._session_file = os.path.join(os.path.expanduser("~"), ".custodian_claude_session")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("NAI WORKBENCH — ADMIN 01", id="title-bar")

        with TabbedContent("Projects", "Custodian", "Fossils", "Detective", "Status", "Editor"):
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
                                placeholder="Ask Claude...",
                                id="chat-input",
                            )
                            yield Button("Send", variant="success", id="btn-claude-send")
                            yield Button("Stop", variant="error", id="btn-claude-stop")

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

    def _load_projects(self):
        """Load projects and populate all select widgets."""
        self._projects = get_projects()
        options = [(p["name"], p["id"]) for p in self._projects]

        for select_id in ["custodian-project-select", "fossil-project-select", "detective-project-select", "editor-project-select"]:
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

    def _refresh_status_tab(self):
        """Refresh the status tab."""
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
            self._do_detective("sonnet", det_select.value)
        elif button_id == "btn-detective-deep":
            det_select = self.query_one("#detective-project-select", Select)
            self._do_detective("opus", det_select.value)
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

    # ── Editor Tab ──────────────────────────────────────────────────

    def _init_editor_tab(self):
        """Initialize editor tab: load saved Claude session if any."""
        session = self._load_claude_session()
        label = self.query_one("#session-label", Static)
        if session:
            self._claude_session_id = session["session_id"]
            label.update(f"Session: {self._claude_session_id[:12]}... (saved)")
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
        """Generate a new Claude session UUID."""
        self._claude_session_id = str(uuid.uuid4())
        self._save_claude_session(self._claude_session_id)

        label = self.query_one("#session-label", Static)
        label.update(f"Session: {self._claude_session_id[:12]}... [green]ready[/green]")

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.clear()
        chat_log.write(f"[bold green]Developer session ready[/bold green] ({self._claude_session_id[:8]})")
        chat_log.write("")
        chat_log.write("[bold]Claude is your on-demand developer. Tell it what to build, fix, or change.[/bold]")
        chat_log.write("[dim]Tools: Read, Edit, Write, Bash, Glob, Grep + Custodian MCP (fossils, symbols, insights)[/dim]")
        chat_log.write("[dim]Open a file in the tree → Claude sees it as context. Editor auto-reloads after edits.[/dim]")
        chat_log.write("[dim]Session persists across restarts. Use 'Resume' to continue later.[/dim]")
        chat_log.write("")

    def _do_resume_claude_session(self):
        """Resume a previously saved Claude session."""
        session = self._load_claude_session()
        if not session:
            self.notify("No saved session found", severity="warning")
            return

        self._claude_session_id = session["session_id"]
        label = self.query_one("#session-label", Static)
        label.update(f"Session: {self._claude_session_id[:12]}... (resumed)")

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.write(
            f"[bold blue]Resumed session: {self._claude_session_id}[/bold blue]\n"
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
        """Send the chat input to Claude."""
        chat_input = self.query_one("#chat-input", Input)
        message = chat_input.value.strip()
        if not message:
            return

        if self._claude_running:
            self.notify("Claude is still running — wait or click Stop", severity="warning")
            return

        # Auto-create session if none exists
        if not self._claude_session_id:
            self._do_new_claude_session()

        chat_log = self.query_one("#claude-chat-log", RichLog)
        chat_log.write(f"\n[bold cyan]You:[/bold cyan] {message}")
        chat_input.value = ""

        # Show working indicator
        session_label = self.query_one("#session-label", Static)
        session_label.update(f"Session: {self._claude_session_id[:12]}... [bold yellow]working...[/bold yellow]")

        prompt = self._build_claude_prompt(message)
        self._run_claude_query(prompt)

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in chat input."""
        if event.input.id == "chat-input":
            self._do_send_claude_message()

    def _extract_text_from_event(self, event_data):
        """Extract displayable text from a Claude stream-json event.

        Also tracks which files Claude edits/writes so we can auto-reload.
        """
        t = event_data.get("type", "")
        subtype = event_data.get("subtype", "")

        # Streaming text delta
        if t == "content_block_delta":
            delta = event_data.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                return delta.get("text", "")
            # input_json_delta for tool params — skip (noisy)
            return ""

        # Direct text field (assistant text chunk)
        if t == "assistant" and subtype == "text":
            return event_data.get("text", "")

        # Full message with content array
        if t == "assistant" and "message" in event_data:
            texts = []
            for block in event_data["message"].get("content", []):
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "unknown")
                        inp = block.get("input", {})
                        fp = inp.get("file_path", inp.get("path", ""))
                        if fp:
                            self._claude_edited_files.add(fp)
                            texts.append(f"\n[bold magenta]>>> {name}[/bold magenta] {fp}\n")
                        else:
                            texts.append(f"\n[bold magenta]>>> {name}[/bold magenta]\n")
            return "\n".join(texts)

        # Tool use (top-level event) — Claude is calling a tool
        if t == "assistant" and subtype == "tool_use":
            tool = event_data.get("name", event_data.get("tool", "unknown"))
            tool_input = event_data.get("input", {})
            fp = tool_input.get("file_path", tool_input.get("path", ""))
            if fp and tool in ("Edit", "Write", "NotebookEdit"):
                self._claude_edited_files.add(fp)
            # Color by operation type
            if tool in ("Edit", "Write", "NotebookEdit"):
                color = "bold red"  # write ops in red
            elif tool in ("Read", "Glob", "Grep"):
                color = "bold blue"  # read ops in blue
            elif tool == "Bash":
                color = "bold yellow"  # shell ops in yellow
            elif tool.startswith("mcp__"):
                color = "bold cyan"  # MCP tools in cyan
                tool = tool.replace("mcp__custodian__", "fossil:")
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
            # For MCP and other tools, show key params
            params = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:3] if v)
            if params:
                return f"\n[{color}]>>> {tool}[/{color}] ({params})\n"
            return f"\n[{color}]>>> {tool}[/{color}]\n"

        # Content block start (tool_use or text)
        if t == "content_block_start":
            block = event_data.get("content_block", {})
            if block.get("type") == "tool_use":
                tool = block.get("name", "unknown")
                return f"\n[bold magenta]>>> {tool}[/bold magenta] "
            return ""

        # Tool result — show success/failure
        if t == "tool_result":
            content = event_data.get("content", "")
            is_error = event_data.get("is_error", False)
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            if isinstance(content, str):
                content = content.strip()
                if len(content) > 500:
                    content = content[:500] + "..."
            if is_error:
                return f"[bold red]ERROR:[/bold red] [red]{content}[/red]\n"
            if content:
                # Show first few lines of output, dimmed
                lines = content.split("\n")
                if len(lines) > 8:
                    preview = "\n".join(lines[:6])
                    return f"[dim]{preview}\n... ({len(lines)-6} more lines)[/dim]\n"
                return f"[dim]{content}[/dim]\n"
            return "[dim]OK[/dim]\n"

        return ""

    @work(thread=True)
    def _run_claude_query(self, prompt):
        """Spawn Claude CLI subprocess and stream response to chat log.

        Claude runs with full tool access (Read, Edit, Write, Bash, Glob, Grep).
        We track which files it edits and auto-reload the editor afterward.
        """
        chat_log = self.query_one("#claude-chat-log", RichLog)
        self._claude_running = True
        self._claude_edited_files = set()

        env = os.environ.copy()
        # Remove variables that could interfere with nested Claude invocation
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(key, None)

        # Build system prompt with fossil briefs so Claude knows the project landscape
        briefs = self._get_fossil_briefs()
        system_ctx = EDITOR_SYSTEM_PROMPT + f"\nRegistered projects:\n{briefs}\n"

        # Build MCP config path (absolute so it works from any cwd)
        mcp_config = os.path.join(self._workbench_path, ".claude", "mcp.json")

        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--session-id", self._claude_session_id,
            "--append-system-prompt", system_ctx,
            "--permission-mode", "acceptEdits",
        ]

        # Pass MCP config explicitly if it exists
        if os.path.exists(mcp_config):
            cmd.extend(["--mcp-config", mcp_config])

        claude_cwd = self._editor_project_path or self._workbench_path
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=claude_cwd,
            )
            self._claude_process = proc

            # Send prompt and close stdin
            proc.stdin.write(prompt)
            proc.stdin.close()

            chat_log.write("[bold yellow]Claude:[/bold yellow]")

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

                # Show result/completion metadata
                if event_data.get("type") == "result":
                    cost = event_data.get("cost_usd")
                    duration = event_data.get("duration_ms")
                    meta = []
                    if cost is not None:
                        meta.append(f"${cost:.4f}")
                    if duration is not None:
                        meta.append(f"{duration/1000:.1f}s")
                    if meta:
                        chat_log.write(f"[dim]({', '.join(meta)})[/dim]")

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
            chat_log.write("[red]Claude CLI not found. Is 'claude' on PATH?[/red]")
        except Exception as e:
            chat_log.write(f"[red]Error: {type(e).__name__}: {e}[/red]")
        finally:
            self._claude_running = False
            self._claude_process = None
            # Clear "working" indicator
            try:
                label = self.query_one("#session-label", Static)
                self.call_from_thread(
                    label.update,
                    f"Session: {self._claude_session_id[:12]}... [green]ready[/green]"
                )
            except Exception:
                pass

    def _do_stop_claude(self):
        """Terminate the running Claude process."""
        if self._claude_process and self._claude_running:
            self._claude_running = False
            try:
                self._claude_process.terminate()
            except OSError:
                pass
            chat_log = self.query_one("#claude-chat-log", RichLog)
            chat_log.write("[yellow]Stopped.[/yellow]")
            self.notify("Claude stopped")
        else:
            self.notify("Nothing running", severity="warning")

    # ── Actions ───────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        """Refresh all tabs."""
        self._load_projects()
        self._refresh_projects_tab()
        self._refresh_custodian_tab()
        self._refresh_fossils_tab()
        self._refresh_detective_tab()
        self._refresh_status_tab()
        self.notify("Refreshed")

    def action_focus_tab(self, tab_name: str) -> None:
        """Switch to a specific tab."""
        tabbed = self.query_one(TabbedContent)
        tabbed.active = f"tab-{tab_name}"


if __name__ == "__main__":
    app = CustodianAdmin()
    app.run()
