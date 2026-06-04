#!/usr/bin/env python3
"""NAI Workbench — Desktop notification daemon.

Polls workbench status and sends Windows toast notifications on state changes:
- Indexing started/finished
- Sandbox started/stopped
- New fossil created
- SSH/watchdog issues
- Agent run completed

Pure stdlib. Uses PowerShell for Windows toast notifications (works from WSL too).
"""

import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import URLError

API_BASE = "http://localhost:7777"
POLL_INTERVAL = 5  # seconds


def fetch_json(path):
    """Fetch JSON from the sandbox router API."""
    try:
        req = Request(f"{API_BASE}{path}", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def send_notification(title, message):
    """Send a Windows toast notification via PowerShell."""
    # Escape single quotes for PowerShell
    title = title.replace("'", "''")
    message = message.replace("'", "''")

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, "
        "ContentType = WindowsRuntime] > $null; "
        "$template = @\"\n"
        "<toast>\n"
        "  <visual>\n"
        "    <binding template=\"ToastGeneric\">\n"
        f"      <text>{title}</text>\n"
        f"      <text>{message}</text>\n"
        "    </binding>\n"
        "  </visual>\n"
        "  <audio silent=\"true\"/>\n"
        "</toast>\n"
        "\"@; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml($template); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('NAI Workbench').Show($toast)"
    )

    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # Fallback: try notify-send (Linux native)
        try:
            subprocess.Popen(
                ["notify-send", f"NAI Workbench: {title}", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(f"[notifier] {title}: {message}", file=sys.stderr)


class WorkbenchNotifier:
    """Tracks workbench state and fires notifications on changes."""

    def __init__(self):
        self.indexing_active = False
        self.indexing_project = None
        self.sandbox_active = False
        self.sandbox_project = None
        self.last_fossil_id = None
        self.ssh_status = None
        self.watchdog_stale = False
        self.first_poll = True

    def poll_and_notify(self):
        """Poll the API and send notifications for state changes."""
        wb = fetch_json("/api/workbench")
        if not wb:
            return

        # -- Indexing --
        ix = wb.get("indexing", {})
        ix_active = ix.get("active", False)
        ix_project = ix.get("project", "")
        ix_step = ix.get("step", "")

        if ix_active and not self.indexing_active:
            send_notification("Indexing Started", f"{ix_project} -- {ix_step}")
        elif not ix_active and self.indexing_active:
            send_notification("Indexing Complete", f"{self.indexing_project} finished")

        self.indexing_active = ix_active
        self.indexing_project = ix_project

        # -- Sandbox --
        sb = wb.get("sandbox", {})
        sb_active = sb.get("active", False)
        sb_project = sb.get("project", "")
        sb_port = sb.get("port", "")

        if sb_active and not self.sandbox_active:
            send_notification("Sandbox Started", f"{sb_project} on port {sb_port}")
        elif not sb_active and self.sandbox_active:
            send_notification("Sandbox Stopped", f"{self.sandbox_project}")

        self.sandbox_active = sb_active
        self.sandbox_project = sb_project

        # -- New Fossil --
        fossils = wb.get("fossils", [])
        if fossils:
            newest_id = fossils[0].get("id")
            if self.last_fossil_id is not None and newest_id > self.last_fossil_id:
                f = fossils[0]
                send_notification(
                    "New Fossil",
                    f"{f.get('project', '?')} -- {f.get('symbols', 0)} symbols"
                )
            self.last_fossil_id = newest_id

        # -- SSH/Watchdog --
        wd = wb.get("watchdog")
        if wd:
            sshd = wd.get("sshd", {})
            new_ssh = sshd.get("status")
            if new_ssh and new_ssh != self.ssh_status and not self.first_poll:
                if new_ssh == "recovered":
                    send_notification("SSH Recovered",
                                     f"Recovered {sshd.get('recoveries', 0)} time(s)")
                elif new_ssh not in ("ok",):
                    send_notification("SSH Issue", f"Status: {new_ssh}")
            self.ssh_status = new_ssh

            new_stale = bool(wd.get("_stale"))
            if new_stale and not self.watchdog_stale and not self.first_poll:
                send_notification("Watchdog Stale", "Health file not updating")
            self.watchdog_stale = new_stale

        self.first_poll = False

    def run(self):
        """Main loop."""
        print("[notifier] NAI Workbench notification daemon started", file=sys.stderr)
        # Load poll interval from settings
        settings = fetch_json("/api/ticker-settings")
        interval = POLL_INTERVAL
        if settings:
            try:
                interval = int(settings.get("poll_interval", POLL_INTERVAL))
            except (ValueError, TypeError):
                pass

        while True:
            try:
                self.poll_and_notify()
            except Exception as e:
                print(f"[notifier] error: {e}", file=sys.stderr)
            time.sleep(interval)


if __name__ == "__main__":
    notifier = WorkbenchNotifier()
    notifier.run()
