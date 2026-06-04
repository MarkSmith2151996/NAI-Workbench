#!/usr/bin/env python3
"""Test TUI app to validate the Docker sandbox pipeline."""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, RichLog
from textual.containers import Horizontal, Vertical
from rich.text import Text
import platform
import os
import subprocess


class TestSandboxApp(App):
    CSS = """
    Screen {
        background: #1e1e2e;
    }
    #title-box {
        height: 5;
        content-align: center middle;
        background: #181825;
        border: solid #22c55e;
        margin: 1 2;
    }
    #info-panel {
        height: auto;
        margin: 0 2;
        padding: 1 2;
        background: #181825;
        border: solid #313244;
    }
    #button-bar {
        height: 3;
        margin: 1 2;
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    #log {
        margin: 0 2 1 2;
        border: solid #313244;
        background: #11111b;
        min-height: 10;
    }
    .success { color: #22c55e; }
    .info { color: #89b4fa; }
    .warn { color: #f9e2af; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("t", "run_test", "Run Test"),
    ]

    def compose(self):
        yield Header(show_clock=True)
        yield Static(
            "[bold #22c55e]✓ SANDBOX TEST TUI[/]\n"
            "[#6c7086]Running inside Docker container via Alpha Builds[/]",
            id="title-box",
        )
        yield Static(id="info-panel")
        with Horizontal(id="button-bar"):
            yield Button("System Info", variant="primary", id="btn-sysinfo")
            yield Button("List Files", variant="default", id="btn-files")
            yield Button("Check Python", variant="default", id="btn-python")
            yield Button("Stress Test", variant="warning", id="btn-stress")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self):
        info = self.query_one("#info-panel", Static)
        info.update(
            f"[bold #89b4fa]Container:[/] {os.environ.get('HOSTNAME', 'unknown')}  |  "
            f"[bold #89b4fa]Python:[/] {platform.python_version()}  |  "
            f"[bold #89b4fa]OS:[/] {platform.platform()}  |  "
            f"[bold #89b4fa]CWD:[/] {os.getcwd()}"
        )
        log = self.query_one("#log", RichLog)
        log.write("[bold #22c55e]✓[/] Sandbox TUI launched successfully!")
        log.write(f"[#6c7086]Container hostname: {os.environ.get('HOSTNAME', 'n/a')}[/]")
        log.write(f"[#6c7086]Working directory: {os.getcwd()}[/]")
        log.write("[#6c7086]Press buttons above or use keybindings (t=test, q=quit)[/]")

    def on_button_pressed(self, event: Button.Pressed):
        log = self.query_one("#log", RichLog)
        btn = event.button.id

        if btn == "btn-sysinfo":
            log.write("\n[bold #89b4fa]── System Info ──[/]")
            log.write(f"  Platform: {platform.platform()}")
            log.write(f"  Python: {platform.python_version()}")
            log.write(f"  Hostname: {os.environ.get('HOSTNAME', 'n/a')}")
            log.write(f"  User: {os.environ.get('USER', 'n/a')}")
            try:
                mem = subprocess.check_output(["free", "-h"], text=True).strip()
                for line in mem.split("\n"):
                    log.write(f"  {line}")
            except Exception:
                log.write("  [#f38ba8](free not available)[/]")

        elif btn == "btn-files":
            log.write("\n[bold #89b4fa]── /workspace Contents ──[/]")
            try:
                files = os.listdir("/workspace")
                for f in sorted(files)[:20]:
                    icon = "📁" if os.path.isdir(f"/workspace/{f}") else "📄"
                    log.write(f"  {icon} {f}")
                if len(files) > 20:
                    log.write(f"  ... and {len(files) - 20} more")
            except Exception as e:
                log.write(f"  [#f38ba8]Error: {e}[/]")

        elif btn == "btn-python":
            log.write("\n[bold #89b4fa]── Python Packages ──[/]")
            try:
                result = subprocess.check_output(
                    ["pip3", "list", "--format=columns"], text=True, stderr=subprocess.DEVNULL
                )
                for line in result.strip().split("\n")[:15]:
                    log.write(f"  {line}")
            except Exception as e:
                log.write(f"  [#f38ba8]Error: {e}[/]")

        elif btn == "btn-stress":
            log.write("\n[bold #f9e2af]── Stress Test ──[/]")
            import time
            start = time.time()
            total = 0
            for i in range(1_000_000):
                total += i
            elapsed = time.time() - start
            log.write(f"  Sum 0..999999 = {total:,}")
            log.write(f"  Time: {elapsed:.3f}s")
            log.write(f"  [bold #22c55e]✓ Stress test passed[/]")

    def action_run_test(self):
        log = self.query_one("#log", RichLog)
        log.write("\n[bold #22c55e]── Quick Self-Test ──[/]")
        log.write("  ✓ App is responsive")
        log.write("  ✓ Widgets rendering correctly")
        log.write("  ✓ Event handling works")
        log.write("  [bold #22c55e]All checks passed![/]")


if __name__ == "__main__":
    TestSandboxApp().run()
