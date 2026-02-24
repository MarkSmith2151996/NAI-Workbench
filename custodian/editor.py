#!/usr/bin/env python3
"""Custodian Editor — Project picker + session manager.

Launches Claude CLI with full custodian MCP context, fossil briefs,
and persistent session IDs per project.
"""

import os
import platform
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static, Button, Label

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")

# Detect if running in WSL (Linux but with Windows paths in DB)
_IS_WSL = platform.system() == "Linux" and os.path.exists("/proc/version") and "microsoft" in open("/proc/version").read().lower()


def _to_native_path(path):
    """Convert Windows paths to WSL /mnt/ paths when running in WSL."""
    if not _IS_WSL or not path:
        return path
    # Convert C:\Users\... → /mnt/c/Users/...
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_projects():
    """Get all active projects with fossil + session info."""
    conn = get_db()
    rows = conn.execute("""
        SELECT p.id, p.name, p.path, p.stack, p.last_indexed,
               (SELECT COUNT(*) FROM fossils f WHERE f.project_id = p.id) as fossil_count,
               (SELECT MAX(f.version) FROM fossils f WHERE f.project_id = p.id) as fossil_version,
               (SELECT MAX(f.created_at) FROM fossils f WHERE f.project_id = p.id) as fossil_date,
               (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count,
               (SELECT es.session_id FROM editor_sessions es
                WHERE es.project_id = p.id AND es.status = 'active'
                ORDER BY es.last_active DESC LIMIT 1) as active_session_id,
               (SELECT es.summary FROM editor_sessions es
                WHERE es.project_id = p.id AND es.status = 'active'
                ORDER BY es.last_active DESC LIMIT 1) as session_summary,
               (SELECT es.last_active FROM editor_sessions es
                WHERE es.project_id = p.id AND es.status = 'active'
                ORDER BY es.last_active DESC LIMIT 1) as session_last_active
        FROM projects p
        WHERE p.status = 'active'
        ORDER BY p.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_fossil_brief(project_id):
    """Get a short fossil summary for the system prompt."""
    conn = get_db()
    fossil = conn.execute("""
        SELECT summary, architecture, known_issues
        FROM fossils WHERE project_id = ?
        ORDER BY created_at DESC LIMIT 1
    """, (project_id,)).fetchone()
    conn.close()
    if not fossil:
        return None
    return dict(fossil)


def get_or_create_session(project_id, resume=False):
    """Get active session or create a new one. Returns (session_id, is_new)."""
    conn = get_db()

    if resume:
        row = conn.execute("""
            SELECT session_id FROM editor_sessions
            WHERE project_id = ? AND status = 'active'
            ORDER BY last_active DESC LIMIT 1
        """, (project_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE editor_sessions SET last_active = datetime('now') WHERE session_id = ?",
                (row["session_id"],),
            )
            conn.commit()
            conn.close()
            return row["session_id"], False

    # Create new session
    session_id = str(uuid.uuid4())

    # Mark old sessions as inactive
    conn.execute(
        "UPDATE editor_sessions SET status = 'closed' WHERE project_id = ? AND status = 'active'",
        (project_id,),
    )

    hostname = os.environ.get("HOSTNAME", os.environ.get("COMPUTERNAME", "unknown"))
    conn.execute("""
        INSERT INTO editor_sessions (project_id, session_id, device, status)
        VALUES (?, ?, ?, 'active')
    """, (project_id, session_id, hostname))
    conn.commit()
    conn.close()
    return session_id, True


def get_git_info(project_path):
    """Get branch and dirty file count for a project."""
    if not os.path.isdir(os.path.join(project_path, ".git")):
        return None, 0

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        changed = len(status.splitlines()) if status else 0
        return branch, changed
    except Exception:
        return None, 0


def build_system_prompt(project, fossil_brief):
    """Build the full system prompt for the Claude session."""
    parts = []

    parts.append(
        "You are a developer working on the '{}' project.\n"
        "Path: {}\nStack: {}\n".format(
            project["name"], project["path"], project["stack"] or "unknown"
        )
    )

    parts.append("""IMPORTANT: You have 17 MCP tools connected via the "custodian" MCP server. They are ALREADY AVAILABLE — do NOT look for config files, just call them directly. If asked about your tools, call list_projects() to prove they work.

WORKFLOW:
1. Call get_project_fossil('{}') FIRST to load architecture context
2. Use lookup_symbol(project, symbol) for live line numbers (tree-sitter)
3. Read files, make changes with Edit/Write
4. Use sandbox_start/sandbox_test to run and test
5. Use sandbox_logs to check for errors

YOUR MCP TOOLS (call these directly — they are connected and working):

CUSTODIAN (8 knowledge tools):
- list_projects() — registered projects with status
- get_project_fossil(project) — architecture, file tree, dependencies, summary
- lookup_symbol(project, symbol) — live tree-sitter search, current line numbers
- get_symbol_context(project, symbol) — Sonnet's descriptions + relationships
- find_related_files(project, symbol) — files to touch for a change
- get_recent_changes(project) — summarized recent commits
- get_detective_insights(project?) — coupling patterns, warnings
- trigger_custodian(project) — re-index with Sonnet

SANDBOX (6 tools):
- sandbox_start(project, command?) — start dev server (auto-detects npm/python)
- sandbox_stop() — stop running sandbox
- sandbox_restart() — restart sandbox
- sandbox_status() — PID, port, error count
- sandbox_logs(lines?, filter?) — recent output, filter by "error"/"warning"
- sandbox_test(command?) — run test suite (auto-detects npm test/pytest)

PENPOT (3 wireframe tools):
- penpot_list_projects() — list all Penpot designs
- penpot_get_page(file_id, page?) — component names, layout, text content
- penpot_export_svg(file_id, page?) — export wireframe as SVG

RULES:
- Make real code changes. Do not just describe what to do.
- After editing, verify your changes compile/work (use sandbox_test or Bash).
- Keep changes minimal and focused.
- Use fossil/symbol tools before grepping blindly.
""".format(project["name"]))

    if fossil_brief:
        parts.append("PROJECT CONTEXT (from fossil):\n")
        if fossil_brief.get("summary"):
            parts.append(f"Summary: {fossil_brief['summary']}\n")
        if fossil_brief.get("architecture"):
            arch = fossil_brief["architecture"]
            if len(arch) > 1500:
                arch = arch[:1500] + "... (use get_project_fossil for full)"
            parts.append(f"Architecture: {arch}\n")
        if fossil_brief.get("known_issues"):
            issues = fossil_brief["known_issues"]
            if len(issues) > 500:
                issues = issues[:500] + "..."
            parts.append(f"Known Issues: {issues}\n")

    return "\n".join(parts)


def launch_claude(project, session_id, resume, system_prompt):
    """Launch Claude CLI as a child process. Returns when Claude exits."""
    # Reset terminal fully — clear Textual's alternate screen buffer
    sys.stdout.write("\033[?1049l")  # exit alternate screen
    sys.stdout.write("\033[?25h")    # show cursor
    sys.stdout.write("\033[0m")      # reset colors
    sys.stdout.write("\033[2J")      # clear screen
    sys.stdout.write("\033[H")       # cursor home
    sys.stdout.flush()

    # Build args list
    args = ["claude"]

    # Skip permission prompts — editor sessions are trusted dev environments
    args.append("--dangerously-skip-permissions")

    if resume:
        args.extend(["--resume", session_id])
    else:
        args.extend(["--session-id", session_id])

    # MCP tools are configured in user-scope ~/.claude.json (registered via
    # `claude mcp add-json --scope user custodian '...'`). No --mcp-config needed.
    # Using --mcp-config would override/conflict with the working user-scope config.

    args.extend(["--append-system-prompt", system_prompt])

    # Run Claude as child process — returns when Claude exits (double-Esc)
    subprocess.run(args, cwd=project["path"])


# --- TUI ---


class ProjectCard(Static):
    """A project card in the picker."""

    def __init__(self, project, **kwargs):
        super().__init__(**kwargs)
        self.project = project

    def compose(self) -> ComposeResult:
        p = self.project
        branch, changed = get_git_info(_to_native_path(p["path"]))

        # Status line
        git_info = ""
        if branch:
            dirty = f"  {changed} changed" if changed else "  clean"
            git_info = f"  {branch}{dirty}"

        fossil_info = "No fossil"
        if p["fossil_version"]:
            age = ""
            if p["fossil_date"]:
                try:
                    fd = datetime.fromisoformat(p["fossil_date"])
                    days = (datetime.now() - fd).days
                    age = f" ({days}d ago)" if days > 0 else " (today)"
                except ValueError:
                    pass
            fossil_info = f"Fossil v{p['fossil_version']}{age}  Symbols: {p['symbol_count']}"

        session_info = ""
        if p["active_session_id"]:
            summary = p["session_summary"] or "no summary"
            if len(summary) > 60:
                summary = summary[:57] + "..."
            session_info = f'\n  [dim]Last: "{summary}"[/dim]'

        text = (
            f"[bold]{p['name']}[/bold]   [dim]{p['stack'] or ''}[/dim]{git_info}\n"
            f"  {fossil_info}{session_info}"
        )

        yield Label(text, markup=True)


class EditorApp(App):
    """Custodian Editor — Project Picker."""

    CSS = """
    Screen {
        background: $surface;
    }

    #title-bar {
        height: 3;
        background: $primary;
        color: $text;
        content-align: center middle;
        text-style: bold;
        padding: 1;
    }

    #project-list {
        margin: 1 2;
        scrollbar-size: 1 1;
    }

    ProjectCard {
        padding: 1 2;
        margin: 0 0 1 0;
        background: $surface-darken-1;
        border: tall $surface-lighten-1;
        height: auto;
        min-height: 5;
    }

    ProjectCard:hover {
        background: $surface;
        border: tall $accent;
    }

    ProjectCard.-selected {
        background: $primary-darken-2;
        border: tall $accent;
        color: $text;
    }

    #action-bar {
        height: 3;
        dock: bottom;
        background: $panel;
        padding: 0 2;
        margin-top: 1;
    }

    #action-bar Button {
        margin: 0 1;
    }

    #status-line {
        height: 1;
        dock: bottom;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("r", "resume", "Resume Session"),
        Binding("n", "new_session", "New Session"),
        Binding("u", "update", "Update Editor"),
        Binding("q", "quit", "Quit"),
        Binding("up,k", "move_up", "Up", show=False),
        Binding("down,j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._projects = []
        self._selected_index = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(" CUSTODIAN EDITOR ", id="title-bar")
        yield VerticalScroll(id="project-list")
        with Horizontal(id="action-bar"):
            yield Button("[R] Resume", variant="primary", id="btn-resume")
            yield Button("[N] New Session", variant="success", id="btn-new")
            yield Button("[U] Update", variant="warning", id="btn-update")
            yield Button("[Q] Quit", variant="error", id="btn-quit")
        yield Static("Select a project to begin", id="status-line")
        yield Footer()

    def on_mount(self):
        self._load_projects()

    def _load_projects(self):
        self._projects = get_projects()
        container = self.query_one("#project-list", VerticalScroll)
        container.remove_children()

        if not self._projects:
            container.mount(Static("[dim]No projects found. Register projects in the Admin TUI.[/dim]"))
            return

        for i, proj in enumerate(self._projects):
            card = ProjectCard(proj, id=f"project-{i}")
            container.mount(card)

        self._update_selection()

    def _update_selection(self):
        cards = self.query("ProjectCard")
        for i, card in enumerate(cards):
            if i == self._selected_index:
                card.add_class("-selected")
            else:
                card.remove_class("-selected")
                card.scroll_visible()

        if self._projects:
            proj = self._projects[self._selected_index]
            has_session = bool(proj.get("active_session_id"))
            status = f"{proj['name']}  —  {'[R]esume or [N]ew session' if has_session else '[N]ew session'}"
            self.query_one("#status-line", Static).update(status)

    def action_move_up(self):
        if self._projects and self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()

    def action_move_down(self):
        if self._projects and self._selected_index < len(self._projects) - 1:
            self._selected_index += 1
            self._update_selection()

    def action_select(self):
        """Enter pressed — resume if session exists, else new."""
        if not self._projects:
            return
        proj = self._projects[self._selected_index]
        if proj.get("active_session_id"):
            self._launch(resume=True)
        else:
            self._launch(resume=False)

    def action_resume(self):
        if not self._projects:
            return
        proj = self._projects[self._selected_index]
        if not proj.get("active_session_id"):
            self.notify("No active session to resume — starting new", severity="warning")
        self._launch(resume=bool(proj.get("active_session_id")))

    def action_new_session(self):
        if not self._projects:
            return
        self._launch(resume=False)

    def action_update(self):
        """Pull latest changes from GitHub and restart."""
        workbench_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.query_one("#status-line", Static).update("Updating from GitHub...")
        self.notify("Pulling latest from GitHub...", severity="information")

        def _do_update():
            try:
                result = subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=workbench_dir,
                    capture_output=True, text=True, timeout=30,
                )
                output = result.stdout.strip()
                if result.returncode != 0:
                    return f"Git pull failed: {result.stderr.strip()}"
                return output
            except Exception as e:
                return f"Update error: {e}"

        import threading

        def _on_done(output):
            if "Already up to date" in output:
                self.notify("Already up to date", severity="information")
                self.query_one("#status-line", Static).update("Already up to date")
            elif "error" in output.lower() or "failed" in output.lower():
                self.notify(output[:80], severity="error")
                self.query_one("#status-line", Static).update(output[:60])
            else:
                self.notify(f"Updated! {output[:60]}", severity="information")
                self.query_one("#status-line", Static).update(
                    "Updated! Restart editor (Q then reopen) for changes to take effect"
                )
                # Reload project list in case DB schema changed
                self._load_projects()

        def _run():
            output = _do_update()
            self.call_from_thread(_on_done, output)

        threading.Thread(target=_run, daemon=True).start()

    def on_button_pressed(self, event: Button.Pressed):
        button_id = event.button.id
        if button_id == "btn-resume":
            self.action_resume()
        elif button_id == "btn-new":
            self.action_new_session()
        elif button_id == "btn-update":
            self.action_update()
        elif button_id == "btn-quit":
            self.exit()

    def on_click(self, event):
        """Handle clicking on a project card."""
        for widget in self.query("ProjectCard"):
            if widget is event.widget or widget is event.widget.parent:
                cards = list(self.query("ProjectCard"))
                try:
                    idx = cards.index(widget)
                    self._selected_index = idx
                    self._update_selection()
                except ValueError:
                    pass
                break

    def _launch(self, resume=False):
        if not self._projects:
            return

        proj = self._projects[self._selected_index]

        native_path = _to_native_path(proj["path"])
        if not os.path.isdir(native_path):
            self.notify(f"Path not found: {native_path}", severity="error")
            return

        # Build a copy with native path for launching
        proj_native = dict(proj)
        proj_native["path"] = native_path

        # Get or create session
        session_id, is_new = get_or_create_session(proj["id"], resume=resume)

        # Build system prompt
        fossil_brief = get_fossil_brief(proj["id"])
        system_prompt = build_system_prompt(proj, fossil_brief)

        # Return launch info — the __main__ block will handle launching
        # after the TUI is fully shut down
        self.exit(result={
            "project": proj_native,
            "session_id": session_id,
            "resume": resume and not is_new,
            "system_prompt": system_prompt,
            "fossil_brief": fossil_brief,
        })


if __name__ == "__main__":
    # Ensure DB tables exist (safe to run multiple times)
    try:
        conn = get_db()
        conn.execute("SELECT 1 FROM editor_sessions LIMIT 1")
        conn.close()
    except sqlite3.OperationalError:
        init_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "init_db.py")
        subprocess.run([sys.executable, init_script])

    # Main loop: picker → Claude → picker → Claude → ...
    # Double-Esc in Claude exits back to the picker. Q in picker quits entirely.
    while True:
        app = EditorApp()
        result = app.run()

        # If user quit (Q) or closed without selecting, exit
        if not result or not isinstance(result, dict) or "project" not in result:
            break

        proj = result["project"]
        action = "Resuming" if result["resume"] else "Starting new"
        print(f"\n{action} session for {proj['name']}...")
        print(f"Session: {result['session_id'][:8]}...")
        print(f"Path: {proj['path']}")
        fb = result.get("fossil_brief")
        if fb:
            print(f"Fossil: {fb.get('summary', '')[:80]}")
        print()

        launch_claude(
            proj,
            result["session_id"],
            result["resume"],
            result["system_prompt"],
        )

        # Claude exited (double-Esc) — loop back to picker
        print("\n[Returning to project picker...]\n")
