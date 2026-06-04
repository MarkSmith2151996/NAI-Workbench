#!/usr/bin/env python3
"""Wave control panel for Custodian sidecar health and restart actions."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Static


DEFAULT_SIDECAR_URL = os.environ.get("CUSTODIAN_SIDECAR_URL", "http://127.0.0.1:8224")
SIDE_UNIT_PATH = Path.home() / ".config/systemd/user/custodian-sidecar.service"
TOKEN_RE = re.compile(r"^Environment=SIDECAR_TOKEN=(.+)$")


def _load_sidecar_token() -> str | None:
    env_token = os.environ.get("SIDECAR_TOKEN")
    if env_token:
        return env_token
    try:
        for line in SIDE_UNIT_PATH.read_text(encoding="utf-8").splitlines():
            match = TOKEN_RE.match(line.strip())
            if match:
                return match.group(1)
    except OSError:
        return None
    return None


class SidecarError(RuntimeError):
    pass


class SidecarClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token = _load_sidecar_token()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        payload = json.dumps({"tool": name, "arguments": arguments or {}}).encode("utf-8")
        request = Request(
            f"{self.base_url}/tool",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SidecarError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SidecarError(str(exc)) from exc

        result = data.get("result")
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                raise SidecarError(result)
        if isinstance(result, dict):
            return result
        raise SidecarError(f"Unexpected sidecar response: {result!r}")


def status_markup(ok: bool, good: str, bad: str) -> str:
    return f"[bold {'green' if ok else 'red'}]{good if ok else bad}[/]"


def metric_markup(value: float | int | None, suffix: str = "%") -> str:
    if value is None:
        return "[dim]n/a[/]"
    numeric = float(value)
    color = "green"
    if numeric >= 90:
        color = "red"
    elif numeric >= 70:
        color = "yellow"
    return f"[{color}]{numeric:.1f}{suffix}[/]"


def text_or_na(value: object) -> str:
    return str(value) if value not in (None, "") else "n/a"


@dataclass
class Snapshot:
    mcp_health: dict
    mcp_sessions: dict
    ping_tunnel: dict
    box_health_all: dict
    ping_mac: dict
    fba_mcp: dict
    sidecar: dict
    system_vitals: dict


class ControlPanelApp(App):
    TITLE = "Control Panel"
    CSS = """
    Screen {
        background: #111827;
        color: #e5e7eb;
    }
    #grid {
        layout: grid;
        grid-size: 2 3;
        grid-columns: 1fr 1fr;
        grid-rows: auto auto 1fr;
        padding: 1;
        grid-gutter: 1;
    }
    .panel {
        border: round #374151;
        padding: 1;
        background: #0f172a;
    }
    #boxes-panel {
        column-span: 2;
    }
    #actions {
        height: auto;
        dock: bottom;
        padding: 0 1 1 1;
    }
    #status-line {
        height: 1;
        dock: bottom;
        padding: 0 1;
        background: #111827;
        color: #9ca3af;
    }
    .box-row {
        height: auto;
        margin: 0 0 1 0;
    }
    .box-info {
        width: 1fr;
    }
    .box-button {
        width: 12;
    }
    Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("r", "refresh_now", "Refresh"),
        Binding("m", "restart_mcp", "Restart MCP"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.client = SidecarClient(DEFAULT_SIDECAR_URL)
        self.refresh_every_seconds = 8
        self.box_button_projects: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="grid"):
            yield Static("Loading MCP status...", id="mcp-panel", classes="panel")
            yield Static("Loading tunnel status...", id="connectivity-panel", classes="panel")
            with Vertical(id="boxes-panel", classes="panel"):
                yield Static("[bold]Project Boxes[/bold]", id="boxes-title")
                yield VerticalScroll(id="boxes-scroll")
            yield Static("Loading system vitals...", id="system-panel", classes="panel")
        with Horizontal(id="actions"):
            yield Button("Restart MCP", id="restart-mcp", variant="warning")
            yield Button("Refresh", id="refresh-now")
        yield Static("", id="status-line")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_dashboard()
        self.set_interval(self.refresh_every_seconds, self.refresh_dashboard)

    def set_status(self, message: str) -> None:
        self.query_one("#status-line", Static).update(message)

    def fetch_snapshot(self) -> Snapshot:
        return Snapshot(
            mcp_health=self.client.tool("mcp_health"),
            mcp_sessions=self.client.tool("mcp_sessions"),
            ping_tunnel=self.client.tool("ping_tunnel"),
            box_health_all=self.client.tool("box_health_all"),
            ping_mac=self.client.tool("ping_mac"),
            fba_mcp=self.client.tool("service_check", {"host": "127.0.0.1", "port": 8090, "path": "/", "timeout": 3}),
            sidecar=self.client.tool("service_check", {"host": "127.0.0.1", "port": 8224, "path": "/", "timeout": 3}),
            system_vitals=self.client.tool("system_vitals"),
        )

    def refresh_dashboard(self) -> None:
        try:
            snapshot = self.fetch_snapshot()
        except SidecarError as exc:
            message = f"Sidecar unavailable: {exc}"
            self.query_one("#mcp-panel", Static).update(f"[bold red]MCP Server[/]\n{message}")
            self.query_one("#connectivity-panel", Static).update(f"[bold red]Connectivity[/]\n{message}")
            self.query_one("#boxes-panel", Static).update(f"[bold red]Project Boxes[/]\n{message}")
            self.query_one("#system-panel", Static).update(f"[bold red]System[/]\n{message}")
            self.set_status(message)
            return

        sessions = snapshot.mcp_sessions.get("sessions", [])
        active_sessions = [item for item in sessions if item.get("status") == "active" and not item.get("stale")]
        stale_sessions = [item for item in sessions if item.get("stale")]
        mcp = snapshot.mcp_health
        self.query_one("#mcp-panel", Static).update(
            "\n".join(
                [
                    "[bold]MCP Server[/bold]",
                    f"Status: {status_markup(bool(mcp.get('alive')), 'RUNNING', 'DOWN')}",
                    f"PID: {text_or_na(mcp.get('pid'))}",
                    f"Port: {text_or_na(mcp.get('port'))}",
                    f"Uptime: {text_or_na(mcp.get('uptime_seconds'))}s",
                    f"Active sessions: [bold]{len(active_sessions)}[/]",
                    f"Stale sessions: [yellow]{len(stale_sessions)}[/]",
                ]
            )
        )

        tunnel = snapshot.ping_tunnel
        mac = snapshot.ping_mac
        fba = snapshot.fba_mcp
        sidecar = snapshot.sidecar
        self.query_one("#connectivity-panel", Static).update(
            "\n".join(
                [
                    "[bold]Connectivity[/bold]",
                    f"Tunnel: {status_markup(bool(tunnel.get('process_alive')), 'ALIVE', 'DOWN')} (pid {text_or_na(tunnel.get('pid'))})",
                    f"Mac: {status_markup(bool(mac.get('reachable')), 'REACHABLE', 'UNREACHABLE')} ({text_or_na(mac.get('latency_ms'))} ms)",
                    f"FBA MCP : {status_markup(bool(fba.get('reachable')), 'UP', 'DOWN')} ({text_or_na(fba.get('http_status'))})",
                    f"Sidecar: {status_markup(bool(sidecar.get('reachable')), 'UP', 'DOWN')} ({text_or_na(sidecar.get('http_status'))})",
                ]
            )
        )

        vitals = snapshot.system_vitals
        self.query_one("#system-panel", Static).update(
            "\n".join(
                [
                    "[bold]System[/bold]",
                    f"CPU load 1m: {text_or_na(vitals.get('cpu_load_1m'))}",
                    f"CPU load 5m: {text_or_na(vitals.get('cpu_load_5m'))}",
                    f"Memory: {metric_markup(vitals.get('memory_pct'))} ({text_or_na(vitals.get('memory_used_mb'))}/{text_or_na(vitals.get('memory_total_mb'))} MB)",
                    f"Disk: {metric_markup(vitals.get('disk_pct'))} ({text_or_na(vitals.get('disk_used_gb'))}/{text_or_na(vitals.get('disk_total_gb'))} GB)",
                ]
            )
        )

        self.render_boxes(snapshot.box_health_all.get("containers", []))
        self.set_status(f"Last refresh {time.strftime('%H:%M:%S')} from {DEFAULT_SIDECAR_URL}")

    def render_boxes(self, containers: list[dict]) -> None:
        self.query_one("#boxes-title", Static).update("[bold]Project Boxes[/bold]")
        scroll = self.query_one("#boxes-scroll", VerticalScroll)
        scroll.remove_children()
        self.box_button_projects.clear()

        if not containers:
            scroll.mount(Static("[dim]No containers reported by sidecar.[/]"))
            return

        for container in containers:
            status = str(container.get("status") or "unknown")
            color = "green" if "Up" in status or "running" in status.lower() else "red"
            if "unhealthy" in status.lower():
                color = "yellow"
            project = str(container.get("project") or container.get("name") or "unknown")
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
            button_id = f"restart-box-{safe_id}"
            self.box_button_projects[button_id] = project

            info = (
                f"[{color}]{project}[/] | {status} | uptime {text_or_na(container.get('uptime'))} | "
                f"mem {text_or_na(container.get('memory_mb'))} MB | ports {text_or_na(container.get('ports'))}"
            )
            row = Horizontal(
                Static(info, classes="box-info"),
                Button("Restart", id=button_id, classes="box-button", variant="primary"),
                classes="box-row",
            )
            scroll.mount(row)

    def restart_mcp_with_wait(self) -> None:
        self.set_status("Restarting MCP server...")
        self.client.tool("mcp_restart")
        deadline = time.time() + 30
        while time.time() < deadline:
            health = self.client.tool("mcp_health")
            if health.get("alive"):
                self.refresh_dashboard()
                self.set_status("MCP server restarted successfully.")
                return
            time.sleep(1)
        self.refresh_dashboard()
        self.set_status("Timed out waiting for MCP server to come back.")

    def restart_box_with_wait(self, project: str) -> None:
        self.set_status(f"Restarting box {project}...")
        self.client.tool("box_restart", {"project": project})
        deadline = time.time() + 30
        while time.time() < deadline:
            boxes = self.client.tool("box_health_all").get("containers", [])
            match = next((box for box in boxes if str(box.get("project")) == project), None)
            if match and "Up" in str(match.get("status") or ""):
                self.refresh_dashboard()
                self.set_status(f"Box {project} restarted successfully.")
                return
            time.sleep(1)
        self.refresh_dashboard()
        self.set_status(f"Timed out waiting for box {project}.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        try:
            if button_id == "restart-mcp":
                self.restart_mcp_with_wait()
            elif button_id == "refresh-now":
                self.refresh_dashboard()
            elif button_id in self.box_button_projects:
                self.restart_box_with_wait(self.box_button_projects[button_id])
        except SidecarError as exc:
            self.set_status(f"Action failed: {exc}")

    def action_refresh_now(self) -> None:
        self.refresh_dashboard()

    def action_restart_mcp(self) -> None:
        self.restart_mcp_with_wait()


if __name__ == "__main__":
    ControlPanelApp().run()
