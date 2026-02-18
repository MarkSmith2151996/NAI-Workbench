#!/usr/bin/env python3
"""NAI Workbench — Textual TUI Dashboard.

A real-time ops dashboard for the NAI Workbench environment.
Displays service health, Docker containers, system metrics,
tmux sessions, and project status.

Modeled after BjTrader's bloomberg.py Grid pattern.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psutil
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, RichLog, Static, TabbedContent, TabPane


# ── Colors (Bloomberg-inspired, matching BjTrader) ──────────────────────

class Colors:
    BG = "#0a0a0a"
    BG_PANEL = "#111111"
    BORDER = "#333333"

    ORANGE = "#ff9800"
    AMBER = "#ffb300"
    GREEN = "#00c853"
    RED = "#f44336"
    CYAN = "#00bcd4"
    BLUE = "#2196f3"
    WHITE = "#ffffff"
    GRAY = "#888888"
    DIM = "#555555"


# ── Service Definitions ─────────────────────────────────────────────────

SERVICES = [
    {"name": "Penpot", "port": 9001, "label": "Whiteboard"},
    {"name": "Komodo", "port": 9090, "label": "Dashboard"},
    {"name": "code-server", "port": 9091, "label": "VS Code"},
]

PROJECTS_DIR = Path.home() / "projects"
WORKBENCH_DIR = Path.home() / "projects" / "nai-workbench"


# ── Service Panel ───────────────────────────────────────────────────────

class ServicePanel(Static):
    """Displays HTTP health status for each service."""

    services = reactive([])

    def render(self) -> Text:
        text = Text()
        text.append(" SERVICES\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 40 + "\n", style=Colors.BORDER)

        if not self.services:
            text.append("  Checking...\n", style=Colors.DIM)
            return text

        for svc in self.services:
            name = svc["name"]
            port = svc["port"]
            status = svc.get("status", "unknown")
            latency = svc.get("latency", 0)

            if status == "up":
                dot = "●"
                dot_style = f"bold {Colors.GREEN}"
                info = f" {latency:>3}ms"
                info_style = Colors.DIM
            elif status == "down":
                dot = "●"
                dot_style = f"bold {Colors.RED}"
                info = " DOWN"
                info_style = Colors.RED
            else:
                dot = "○"
                dot_style = Colors.DIM
                info = ""
                info_style = Colors.DIM

            text.append(f"  {dot}", style=dot_style)
            text.append(f" {name:<16}", style=Colors.WHITE)
            text.append(f":{port:<6}", style=Colors.DIM)
            text.append(f"{info}\n", style=info_style)

        return text


# ── Docker Panel ────────────────────────────────────────────────────────

class DockerPanel(Static):
    """Displays running Docker containers."""

    containers = reactive([])

    def render(self) -> Text:
        text = Text()
        text.append("\n DOCKER\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 40 + "\n", style=Colors.BORDER)

        if not self.containers:
            text.append("  No containers running\n", style=Colors.DIM)
            return text

        for c in self.containers:
            name = c.get("name", "?")
            status = c.get("status", "?")
            is_up = "Up" in status

            dot = "●" if is_up else "●"
            dot_style = f"bold {Colors.GREEN}" if is_up else f"bold {Colors.RED}"

            text.append(f"  {dot}", style=dot_style)
            text.append(f" {name:<24}", style=Colors.WHITE)
            text.append(f" {status}\n", style=Colors.DIM)

        return text


# ── Session Panel ───────────────────────────────────────────────────────

class SessionPanel(Static):
    """Displays active tmux sessions."""

    sessions = reactive([])

    def render(self) -> Text:
        text = Text()
        text.append(" TMUX SESSIONS\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 50 + "\n", style=Colors.BORDER)

        if not self.sessions:
            text.append("  No active sessions\n", style=Colors.DIM)
            text.append("\n  Press ", style=Colors.GRAY)
            text.append("n", style=f"bold {Colors.CYAN}")
            text.append(" to create a new session\n", style=Colors.GRAY)
            return text

        for sess in self.sessions:
            name = sess.get("name", "?")
            windows = sess.get("windows", "?")
            attached = sess.get("attached", False)

            badge = " *" if attached else ""
            badge_style = Colors.GREEN if attached else ""

            text.append(f"  ◆ ", style=Colors.CYAN)
            text.append(f"{name:<20}", style=Colors.WHITE)
            text.append(f" {windows} window(s)", style=Colors.DIM)
            if badge:
                text.append(badge, style=f"bold {badge_style}")
            text.append("\n")

        return text


# ── Project Panel ───────────────────────────────────────────────────────

class ProjectPanel(Static):
    """Displays git projects in ~/projects."""

    projects = reactive([])

    def render(self) -> Text:
        text = Text()
        text.append(" PROJECTS\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 50 + "\n", style=Colors.BORDER)

        if not self.projects:
            text.append("  No projects found\n", style=Colors.DIM)
            return text

        for proj in self.projects:
            name = proj.get("name", "?")
            branch = proj.get("branch", "")
            changes = proj.get("changes", 0)
            has_git = proj.get("has_git", False)

            text.append(f"  ◆ ", style=Colors.BLUE)
            text.append(f"{name:<20}", style=Colors.WHITE)

            if has_git:
                text.append(f" {branch}", style=Colors.CYAN)
                if changes > 0:
                    text.append(f"  {changes} changed", style=Colors.AMBER)
            else:
                text.append(" (no git)", style=Colors.DIM)

            text.append("\n")

        return text


# ── System Panel (sidebar) ──────────────────────────────────────────────

class SystemPanel(Static):
    """Displays CPU, RAM, and disk usage with bar charts."""

    cpu = reactive(0.0)
    ram_used = reactive(0.0)
    ram_total = reactive(0.0)
    disk_used = reactive(0.0)
    disk_total = reactive(0.0)

    def _bar(self, pct: float, width: int = 12) -> tuple[str, str]:
        """Return (bar_string, color) for a percentage."""
        filled = int(pct / 100 * width)
        empty = width - filled
        bar = "█" * filled + "░" * empty
        if pct > 85:
            color = Colors.RED
        elif pct > 60:
            color = Colors.AMBER
        else:
            color = Colors.GREEN
        return bar, color

    def render(self) -> Text:
        text = Text()
        text.append(" SYSTEM\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 22 + "\n", style=Colors.BORDER)

        # CPU
        cpu_bar, cpu_color = self._bar(self.cpu)
        text.append("  CPU  ", style=Colors.GRAY)
        text.append(f"{cpu_bar}", style=cpu_color)
        text.append(f" {self.cpu:4.0f}%\n", style=Colors.WHITE)

        # RAM
        ram_pct = (self.ram_used / self.ram_total * 100) if self.ram_total > 0 else 0
        ram_bar, ram_color = self._bar(ram_pct)
        text.append("  RAM  ", style=Colors.GRAY)
        text.append(f"{ram_bar}", style=ram_color)
        text.append(f" {self.ram_used:.1f}/{self.ram_total:.0f}G\n", style=Colors.WHITE)

        # Disk
        disk_pct = (self.disk_used / self.disk_total * 100) if self.disk_total > 0 else 0
        disk_bar, disk_color = self._bar(disk_pct)
        text.append("  Disk ", style=Colors.GRAY)
        text.append(f"{disk_bar}", style=disk_color)
        text.append(f" {self.disk_used:.0f}/{self.disk_total:.0f}G\n", style=Colors.WHITE)

        return text


# ── Quick Actions Panel (sidebar) ───────────────────────────────────────

class QuickActions(Static):
    """Shows keybinding reference for quick actions."""

    def render(self) -> Text:
        text = Text()
        text.append("\n QUICK ACTIONS\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 22 + "\n", style=Colors.BORDER)

        actions = [
            ("c", "Claude CLI"),
            ("n", "New Session"),
            ("k", "Kill Session"),
            ("r", "Refresh All"),
        ]

        for key, label in actions:
            text.append(f"  [{key}]", style=f"bold {Colors.CYAN}")
            text.append(f" {label}\n", style=Colors.GRAY)

        return text


# ── Uptime Panel (sidebar) ──────────────────────────────────────────────

class UptimePanel(Static):
    """Shows system uptime and current time."""

    boot_time = reactive(0.0)
    clock = reactive("")

    def render(self) -> Text:
        text = Text()
        text.append("\n UPTIME\n", style=f"bold {Colors.ORANGE}")
        text.append("─" * 22 + "\n", style=Colors.BORDER)

        if self.boot_time > 0:
            uptime_secs = time.time() - self.boot_time
            hours = int(uptime_secs // 3600)
            minutes = int((uptime_secs % 3600) // 60)
            text.append(f"  {hours}h {minutes}m\n", style=Colors.WHITE)

        text.append(f"\n  {self.clock}\n", style=Colors.DIM)

        return text


# ── Main App ────────────────────────────────────────────────────────────

class WorkbenchDashboard(App):
    """NAI Workbench TUI Dashboard."""

    TITLE = "NAI WORKBENCH"
    SUB_TITLE = "Ops Dashboard"

    CSS = """
    Screen {
        background: #0a0a0a;
    }

    Header {
        dock: top;
        background: #111111;
        color: #ff9800;
    }

    Footer {
        dock: bottom;
        background: #111111;
    }

    #main-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 28;
        grid-gutter: 0;
        padding: 0;
        margin: 0;
    }

    TabbedContent {
        background: #0a0a0a;
        height: 100%;
    }

    TabPane {
        padding: 0 1;
    }

    ContentSwitcher {
        background: #0a0a0a;
    }

    Tab {
        background: #111111;
        color: #888888;
    }

    Tab.-active {
        background: #1a1a1a;
        color: #ff9800;
    }

    Underline > .underline--bar {
        color: #ff9800;
        background: #333333;
    }

    #sidebar {
        background: #0a0a0a;
        border-left: solid #333333;
        height: 100%;
        padding: 0;
    }

    ServicePanel, DockerPanel, SessionPanel, ProjectPanel {
        height: auto;
        padding: 0;
        margin: 0;
    }

    SystemPanel, QuickActions, UptimePanel {
        height: auto;
        padding: 0;
        margin: 0;
    }

    #event-log {
        height: 100%;
        background: #0a0a0a;
        border: none;
    }
    """

    BINDINGS = [
        Binding("s", "switch_tab('status')", "Status", priority=True),
        Binding("t", "switch_tab('sessions')", "Sessions", priority=True),
        Binding("p", "switch_tab('projects')", "Projects", priority=True),
        Binding("l", "switch_tab('log')", "Log", priority=True),
        Binding("c", "launch_claude", "Claude", priority=True),
        Binding("n", "new_session", "New Session"),
        Binding("k", "kill_session", "Kill Session"),
        Binding("r", "refresh_all", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid(id="main-grid"):
            with TabbedContent(id="tabs"):
                with TabPane("Status", id="status"):
                    yield ServicePanel(id="svc-panel")
                    yield DockerPanel(id="docker-panel")
                with TabPane("Sessions", id="sessions"):
                    yield SessionPanel(id="session-panel")
                with TabPane("Projects", id="projects"):
                    yield ProjectPanel(id="project-panel")
                with TabPane("Log", id="log"):
                    yield RichLog(id="event-log", highlight=True, markup=True)
            with Vertical(id="sidebar"):
                yield SystemPanel(id="sys-panel")
                yield QuickActions(id="actions-panel")
                yield UptimePanel(id="uptime-panel")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize dashboard and start background workers."""
        self._log(f"[bold {Colors.ORANGE}]NAI Workbench Dashboard started[/]")
        self._log(f"[{Colors.DIM}]Press 'q' to quit, 'c' for Claude CLI[/]")

        # Set boot time
        self.query_one("#uptime-panel", UptimePanel).boot_time = psutil.boot_time()

        # Start recurring checks
        self.set_interval(10, self._check_services)
        self.set_interval(15, self._check_docker)
        self.set_interval(5, self._check_system)
        self.set_interval(1, self._update_clock)
        self.set_interval(30, self._check_sessions)
        self.set_interval(60, self._check_projects)

        # Initial data load
        self._check_services()
        self._check_docker()
        self._check_system()
        self._check_sessions()
        self._check_projects()
        self._update_clock()

    def _log(self, msg: str) -> None:
        """Write a timestamped message to the event log."""
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            log = self.query_one("#event-log", RichLog)
            log.write(f"[{Colors.DIM}]{ts}[/] {msg}")
        except Exception:
            pass

    # ── Background Workers ──────────────────────────────────────────────

    @work(thread=True)
    def _check_services(self) -> None:
        """HTTP health check for each service."""
        results = []
        for svc in SERVICES:
            port = svc["port"]
            name = svc["name"]
            try:
                start = time.monotonic()
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(f"http://localhost:{port}")
                latency = int((time.monotonic() - start) * 1000)
                results.append({
                    "name": name,
                    "port": port,
                    "status": "up",
                    "latency": latency,
                })
            except Exception:
                results.append({
                    "name": name,
                    "port": port,
                    "status": "down",
                    "latency": 0,
                })

        def update():
            panel = self.query_one("#svc-panel", ServicePanel)
            old = panel.services
            panel.services = results

            # Log changes
            if old:
                old_map = {s["name"]: s["status"] for s in old}
                for svc in results:
                    prev = old_map.get(svc["name"])
                    if prev and prev != svc["status"]:
                        if svc["status"] == "up":
                            self._log(f"[{Colors.GREEN}]● {svc['name']} is UP[/]")
                        else:
                            self._log(f"[{Colors.RED}]● {svc['name']} is DOWN[/]")

        self.call_from_thread(update)

    @work(thread=True)
    def _check_docker(self) -> None:
        """List running Docker containers."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=10
            )
            containers = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split("\t", 1)
                    name = parts[0] if parts else "?"
                    status = parts[1] if len(parts) > 1 else "?"
                    containers.append({"name": name, "status": status})
        except Exception:
            containers = []

        def update():
            self.query_one("#docker-panel", DockerPanel).containers = containers

        self.call_from_thread(update)

    @work(thread=True)
    def _check_system(self) -> None:
        """Gather CPU, RAM, and disk metrics via psutil."""
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        ram_used = mem.used / (1024 ** 3)
        ram_total = mem.total / (1024 ** 3)
        disk_used = disk.used / (1024 ** 3)
        disk_total = disk.total / (1024 ** 3)

        def update():
            panel = self.query_one("#sys-panel", SystemPanel)
            panel.cpu = cpu
            panel.ram_used = ram_used
            panel.ram_total = ram_total
            panel.disk_used = disk_used
            panel.disk_total = disk_total

        self.call_from_thread(update)

    @work(thread=True)
    def _check_sessions(self) -> None:
        """List active tmux sessions."""
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F",
                 "#{session_name}\t#{session_windows}\t#{session_attached}"],
                capture_output=True, text=True, timeout=5
            )
            sessions = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split("\t")
                    sessions.append({
                        "name": parts[0] if parts else "?",
                        "windows": parts[1] if len(parts) > 1 else "?",
                        "attached": parts[2] == "1" if len(parts) > 2 else False,
                    })
        except Exception:
            sessions = []

        def update():
            self.query_one("#session-panel", SessionPanel).sessions = sessions

        self.call_from_thread(update)

    @work(thread=True)
    def _check_projects(self) -> None:
        """Scan ~/projects for git repos."""
        projects = []
        try:
            if PROJECTS_DIR.exists():
                for d in sorted(PROJECTS_DIR.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        proj = {"name": d.name, "has_git": False, "branch": "", "changes": 0}
                        git_dir = d / ".git"
                        if git_dir.exists():
                            proj["has_git"] = True
                            try:
                                branch = subprocess.run(
                                    ["git", "-C", str(d), "branch", "--show-current"],
                                    capture_output=True, text=True, timeout=5
                                )
                                proj["branch"] = branch.stdout.strip() or "?"
                            except Exception:
                                proj["branch"] = "?"
                            try:
                                status = subprocess.run(
                                    ["git", "-C", str(d), "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=5
                                )
                                proj["changes"] = len([
                                    l for l in status.stdout.strip().split("\n") if l.strip()
                                ])
                            except Exception:
                                proj["changes"] = 0
                        projects.append(proj)
        except Exception:
            pass

        def update():
            self.query_one("#project-panel", ProjectPanel).projects = projects

        self.call_from_thread(update)

    def _update_clock(self) -> None:
        """Update the clock display."""
        now = datetime.now().strftime("%H:%M:%S  %a %b %d")
        self.query_one("#uptime-panel", UptimePanel).clock = now

    # ── Actions ─────────────────────────────────────────────────────────

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to a specific tab."""
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id

    def action_launch_claude(self) -> None:
        """Launch Claude CLI in a new tmux session."""
        self._log(f"[{Colors.CYAN}]Launching Claude CLI...[/]")
        self._do_launch_claude()

    @work(thread=True)
    def _do_launch_claude(self) -> None:
        """Create a tmux session with Claude CLI."""
        session_name = f"claude-{datetime.now().strftime('%H%M')}"
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", str(WORKBENCH_DIR)],
                capture_output=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "claude", "Enter"],
                capture_output=True, timeout=5
            )
            self.call_from_thread(
                self._log,
                f"[{Colors.GREEN}]Session '{session_name}' created with Claude CLI[/]"
            )
            self.call_from_thread(self._check_sessions)
        except Exception as e:
            self.call_from_thread(
                self._log,
                f"[{Colors.RED}]Failed to launch Claude: {e}[/]"
            )

    def action_new_session(self) -> None:
        """Create a new tmux session."""
        self._log(f"[{Colors.CYAN}]Creating new session...[/]")
        self._do_new_session()

    @work(thread=True)
    def _do_new_session(self) -> None:
        """Create a plain tmux session."""
        session_name = f"work-{datetime.now().strftime('%H%M%S')}"
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", str(PROJECTS_DIR)],
                capture_output=True, timeout=5
            )
            self.call_from_thread(
                self._log,
                f"[{Colors.GREEN}]Session '{session_name}' created[/]"
            )
            self.call_from_thread(self._check_sessions)
        except Exception as e:
            self.call_from_thread(
                self._log,
                f"[{Colors.RED}]Failed to create session: {e}[/]"
            )

    def action_kill_session(self) -> None:
        """Kill the most recent non-attached tmux session."""
        self._log(f"[{Colors.AMBER}]Killing last session...[/]")
        self._do_kill_session()

    @work(thread=True)
    def _do_kill_session(self) -> None:
        """Kill the last tmux session."""
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_attached}"],
                capture_output=True, text=True, timeout=5
            )
            sessions = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] == "0":
                        sessions.append(parts[0])

            if sessions:
                target = sessions[-1]
                subprocess.run(
                    ["tmux", "kill-session", "-t", target],
                    capture_output=True, timeout=5
                )
                self.call_from_thread(
                    self._log,
                    f"[{Colors.AMBER}]Killed session '{target}'[/]"
                )
                self.call_from_thread(self._check_sessions)
            else:
                self.call_from_thread(
                    self._log,
                    f"[{Colors.DIM}]No detached sessions to kill[/]"
                )
        except Exception as e:
            self.call_from_thread(
                self._log,
                f"[{Colors.RED}]Failed to kill session: {e}[/]"
            )

    def action_refresh_all(self) -> None:
        """Force refresh all panels."""
        self._log(f"[{Colors.CYAN}]Refreshing all panels...[/]")
        self._check_services()
        self._check_docker()
        self._check_system()
        self._check_sessions()
        self._check_projects()

    def action_quit(self) -> None:
        """Quit the dashboard."""
        self.exit()


# ── Entry Point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = WorkbenchDashboard()
    app.run()
