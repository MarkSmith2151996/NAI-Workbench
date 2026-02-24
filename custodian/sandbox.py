#!/usr/bin/env python3
"""NAI Workbench — Sandbox Preview Widget.

Dedicated Wave Terminal widget for sandbox management:
- Shows current sandbox status (project, command, type, port)
- Web apps: embedded log viewer + "open in browser" hint
- Terminal apps: auto-attaches to tmux session for live interaction
- Start/stop controls with project picker
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    RichLog,
    Select,
    Static,
)
from textual.binding import Binding
from textual import work

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
WSH_PATH = "/home/dev/.waveterm/bin/wsh"

# Detect WSL
import platform as _platform
_IS_WSL = (_platform.system() == "Linux"
           and os.path.exists("/proc/version")
           and "microsoft" in open("/proc/version").read().lower())


def _to_native_path(path):
    if not _IS_WSL or not path:
        return path
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
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, path, stack FROM projects WHERE status = 'active' ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_running_sandbox():
    """Get the currently running sandbox, or None."""
    conn = get_db()
    row = conn.execute(
        """SELECT ss.*, p.name as project_name, p.path as project_path
           FROM sandbox_state ss
           JOIN projects p ON p.id = ss.project_id
           WHERE ss.status = 'running'
           ORDER BY ss.id DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def detect_sandbox_command(project_path):
    """Auto-detect dev command. Returns (command, port, app_type)."""
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


# ── CSS ───────────────────────────────────────────────────────────

CSS = """
Screen {
    background: $surface;
}

#title-bar {
    dock: top;
    height: 3;
    background: #22c55e;
    color: $text;
    text-align: center;
    padding: 1;
    text-style: bold;
}

#status-box {
    height: 5;
    border: solid $primary;
    padding: 1;
    margin-bottom: 1;
}

#sandbox-log {
    height: 1fr;
    border: solid $primary;
}

.action-bar {
    height: 3;
    margin-bottom: 1;
}

.project-selector {
    width: 40;
}

#hint-label {
    color: $text-muted;
    padding: 0 1;
}
"""


class SandboxWidget(App):
    """Dedicated sandbox preview/control widget for Wave Terminal."""

    TITLE = "SANDBOX"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "attach", "Attach"),
        Binding("o", "open_browser", "Open Browser"),
    ]

    def __init__(self):
        super().__init__()
        self._sandbox = None  # current sandbox state from DB
        self._log_thread_running = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("SANDBOX", id="title-bar")

        with Vertical():
            with Horizontal(classes="action-bar"):
                yield Select(
                    [],
                    prompt="Select project...",
                    id="project-select",
                    classes="project-selector",
                )
                yield Button("Start", variant="success", id="btn-start")
                yield Button("Stop", variant="error", id="btn-stop")
                yield Button("Attach", variant="primary", id="btn-attach")
                yield Button("Browser", variant="default", id="btn-browser")

            yield Static("Checking sandbox status...", id="status-box")
            yield Static("", id="hint-label")
            yield RichLog(id="sandbox-log", highlight=True, markup=True)

        yield Footer()

    def on_mount(self) -> None:
        self._load_projects()
        self._refresh_status()
        self.set_interval(5, self._refresh_status)

    def _load_projects(self):
        projects = get_projects()
        options = [(p["name"], p["id"]) for p in projects]
        try:
            select = self.query_one("#project-select", Select)
            select.set_options(options)
        except Exception:
            pass

    def _refresh_status(self):
        """Poll DB for current sandbox state and update display."""
        self._sandbox = get_running_sandbox()
        status = self.query_one("#status-box", Static)
        hint = self.query_one("#hint-label", Static)

        if not self._sandbox:
            status.update(
                "[bold dim]No sandbox running[/bold dim]\n"
                "Select a project and click Start"
            )
            hint.update("")
            return

        s = self._sandbox
        preview_type = s.get("preview_type") or "unknown"
        port = s.get("port")
        tmux = s.get("tmux_session")
        cmd = s.get("command") or ""

        type_icon = "[web]" if preview_type == "web" else "[term]" if preview_type == "terminal" else "[?]"
        port_str = f"  port {port}" if port else ""
        tmux_str = f"  tmux: {tmux}" if tmux else ""
        pid_str = f"  PID {s['pid']}" if s.get("pid") else ""

        status.update(
            f"[bold green]● RUNNING[/bold green]  {s['project_name']}\n"
            f"{type_icon} {preview_type}  |  {cmd}{port_str}{pid_str}{tmux_str}"
        )

        if preview_type == "web" and port:
            hint.update(f"[dim]Press [bold]o[/bold] or click Browser → http://localhost:{port}[/dim]")
        elif preview_type == "terminal" and tmux:
            hint.update(f"[dim]Press [bold]a[/bold] or click Attach → tmux session {tmux}[/dim]")
        else:
            hint.update("")

        # Start log streaming if not already running
        if not self._log_thread_running:
            self._stream_logs()

    # ── Button Handlers ──────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-start":
            select = self.query_one("#project-select", Select)
            if isinstance(select.value, int):
                self._do_start(select.value)
            else:
                self.notify("Select a project first", severity="warning")
        elif bid == "btn-stop":
            self._do_stop()
        elif bid == "btn-attach":
            self.action_attach()
        elif bid == "btn-browser":
            self.action_open_browser()

    # ── Start ────────────────────────────────────────────────────

    @work(thread=True)
    def _do_start(self, project_id):
        log = self.query_one("#sandbox-log", RichLog)
        log.clear()

        conn = get_db()
        proj = conn.execute(
            "SELECT id, name, path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.close()

        if not proj:
            log.write("[red]Project not found[/red]")
            return

        project_name = proj["name"]
        project_path = _to_native_path(proj["path"])
        log.write(f"[bold blue]Starting sandbox for {project_name}...[/bold blue]")

        command, port, app_type = detect_sandbox_command(project_path)
        if not command:
            log.write(f"[red]Could not detect dev command for {project_name}[/red]")
            self.call_from_thread(self.notify, "No dev command detected", severity="error")
            return

        log.write(f"Detected: [bold]{command}[/bold] ({app_type})")

        # Stop any existing sandbox first
        existing = get_running_sandbox()
        if existing:
            log.write(f"[yellow]Stopping existing sandbox ({existing['project_name']})...[/yellow]")
            self._stop_sandbox(existing)

        try:
            # ALL sandboxes run in tmux so the viewport can attach
            session_name = f"sandbox-{project_name}"
            subprocess.run(["tmux", "kill-session", "-t", session_name],
                          capture_output=True)

            if app_type == "terminal":
                tmux_cmd = f'{command}; echo "\\n[sandbox exited $?]"; sleep 86400'
            else:
                tmux_cmd = command

            subprocess.run(["tmux", "new-session", "-d", "-s", session_name,
                           "-c", project_path, "bash", "-c", tmux_cmd],
                          capture_output=True)
            log.write(f"[green]Started tmux session: {session_name}[/green]")

            # Get PID from tmux
            pid_result = subprocess.run(
                ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            pid = int(pid_result.stdout.strip()) if pid_result.returncode == 0 and pid_result.stdout.strip() else None

            # Update DB
            conn = get_db()
            conn.execute("DELETE FROM sandbox_state WHERE project_id = ?", (project_id,))
            conn.execute(
                """INSERT INTO sandbox_state
                   (project_id, command, pid, port, status, preview_type, tmux_session)
                   VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (project_id, command, pid, port, app_type, session_name),
            )
            conn.commit()
            conn.close()

            log.write(f"[bold green]Sandbox running for {project_name}[/bold green]")

            if app_type == "web" and port:
                log.write(f"[bold]Browser: http://localhost:{port}[/bold]")
                self._try_wsh_web(port)

            log.write(f"[dim]Sandbox widget will auto-attach to {session_name}[/dim]")

            self.call_from_thread(self._refresh_status)
            self.call_from_thread(self.notify, f"Sandbox started: {project_name}")

        except Exception as e:
            log.write(f"[bold red]Failed: {e}[/bold red]")
            self.call_from_thread(self.notify, f"Start failed: {e}", severity="error")

    def _try_wsh_web(self, port):
        """Try to open a web preview via wsh."""
        if not (os.path.isfile(WSH_PATH) and os.environ.get("WAVETERM_BLOCKID")):
            return
        try:
            subprocess.run([WSH_PATH, "web", "open", "-m", f"http://localhost:{port}"],
                          capture_output=True, timeout=5)
        except Exception:
            pass

    # ── Stop ─────────────────────────────────────────────────────

    @work(thread=True)
    def _do_stop(self):
        log = self.query_one("#sandbox-log", RichLog)
        sandbox = get_running_sandbox()
        if not sandbox:
            log.write("[yellow]No sandbox running[/yellow]")
            self.call_from_thread(self.notify, "Nothing to stop", severity="warning")
            return

        log.write(f"[bold yellow]Stopping {sandbox['project_name']}...[/bold yellow]")
        self._stop_sandbox(sandbox)
        log.write(f"[bold green]Stopped.[/bold green]")
        self._log_thread_running = False
        self.call_from_thread(self._refresh_status)
        self.call_from_thread(self.notify, f"Sandbox stopped: {sandbox['project_name']}")

    def _stop_sandbox(self, sandbox):
        """Stop a sandbox (shared between stop button and start-replaces-existing)."""
        tmux = sandbox.get("tmux_session")
        if tmux:
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                          capture_output=True, timeout=5)

        pid = sandbox.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        conn = get_db()
        conn.execute(
            "UPDATE sandbox_state SET status = 'stopped', pid = NULL, tmux_session = NULL WHERE id = ?",
            (sandbox["id"],),
        )
        conn.commit()
        conn.close()

    # ── Attach (terminal apps) ───────────────────────────────────

    def action_attach(self):
        """Attach to the running tmux sandbox session."""
        sandbox = get_running_sandbox()
        if not sandbox:
            self.notify("No sandbox running", severity="warning")
            return

        tmux = sandbox.get("tmux_session")
        if not tmux:
            self.notify("Not a terminal sandbox — use Browser instead", severity="warning")
            return

        # Use wsh to open a new terminal block attached to the tmux session
        if os.path.isfile(WSH_PATH) and os.environ.get("WAVETERM_BLOCKID"):
            try:
                subprocess.run([WSH_PATH, "run", "-m", "--",
                               "tmux", "attach-session", "-t", tmux],
                              capture_output=True, timeout=5)
                self.notify(f"Attached to {tmux}")
            except Exception as e:
                self.notify(f"Attach failed: {e}", severity="error")
        else:
            self.notify(f"Run: tmux attach -t {tmux}")

    # ── Open Browser (web apps) ──────────────────────────────────

    def action_open_browser(self):
        """Open the web sandbox URL in a Wave browser block."""
        sandbox = get_running_sandbox()
        if not sandbox:
            self.notify("No sandbox running", severity="warning")
            return

        port = sandbox.get("port")
        if not port:
            self.notify("No port — not a web sandbox", severity="warning")
            return

        if os.path.isfile(WSH_PATH) and os.environ.get("WAVETERM_BLOCKID"):
            try:
                subprocess.run([WSH_PATH, "web", "open", "-m", f"http://localhost:{port}"],
                              capture_output=True, timeout=5)
                self.notify(f"Opened http://localhost:{port}")
            except Exception as e:
                self.notify(f"Open failed: {e}", severity="error")
        else:
            self.notify(f"Open http://localhost:{port} in your browser")

    # ── Log Streaming ────────────────────────────────────────────

    @work(thread=True)
    def _stream_logs(self):
        """Stream sandbox logs to the RichLog widget."""
        if self._log_thread_running:
            return
        self._log_thread_running = True
        log = self.query_one("#sandbox-log", RichLog)
        last_content = ""

        while self._log_thread_running:
            sandbox = get_running_sandbox()
            if not sandbox:
                self._log_thread_running = False
                break

            tmux = sandbox.get("tmux_session")
            if tmux:
                # Terminal app — capture tmux pane
                try:
                    result = subprocess.run(
                        ["tmux", "capture-pane", "-t", tmux, "-p", "-S", "-50"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        content = result.stdout.rstrip()
                        # Only redraw if content actually changed
                        if content != last_content:
                            last_content = content
                            log.clear()
                            for line in content.split("\n"):
                                if line.strip():
                                    log.write(line)
                    else:
                        # tmux session gone — stop polling
                        if last_content:
                            log.write("[yellow]tmux session ended[/yellow]")
                        self._log_thread_running = False
                        self.call_from_thread(self._refresh_status)
                        break
                except Exception:
                    pass
            else:
                # Web app started by MCP — we can't monitor PID across processes.
                # Just show a static message; use sandbox_logs MCP tool for output.
                if not last_content:
                    last_content = "web"
                    pid = sandbox.get("pid")
                    port = sandbox.get("port")
                    log.write(f"[bold]Web sandbox running[/bold] (PID {pid})")
                    if port:
                        log.write(f"Serving on http://localhost:{port}")
                    log.write("[dim]Use sandbox_logs() in Claude for live output[/dim]")

            time.sleep(3)

    # ── Actions ──────────────────────────────────────────────────

    def action_refresh(self):
        self._load_projects()
        self._refresh_status()
        log = self.query_one("#sandbox-log", RichLog)
        log.clear()
        self.notify("Refreshed")


if __name__ == "__main__":
    app = SandboxWidget()
    app.run()
