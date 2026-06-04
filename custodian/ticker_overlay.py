#!/usr/bin/env python3
"""NAI Workbench — Bloomberg-style scrolling ticker overlay for Windows.

Always-on-top transparent bar that shows live workbench status:
indexing, sandbox, fossils, agents, projects, watchdog, etc.

Runs via pythonw.exe (no console window). Pure stdlib: tkinter + urllib.
"""

import json
import threading
import time
import tkinter as tk
from urllib.request import urlopen, Request
from urllib.error import URLError

API_BASE = "http://localhost:7777"
SEPARATOR = "  |  "


def fetch_json(path):
    """Fetch JSON from the sandbox router API."""
    try:
        req = Request(f"{API_BASE}{path}", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def build_ticker_text(wb, config):
    """Build a flat text string from workbench status and ticker config."""
    parts = []

    # Claude sessions — the star of the show
    claude_sessions = wb.get("claude_sessions", [])
    if claude_sessions:
        for s in claude_sessions:
            proj = s.get("project", "?")
            # Shorten long project names
            if len(proj) > 20:
                proj = proj[:18] + ".."
            activity = s.get("activity", "active")
            parts.append(f"\u25CF {proj}: {activity}")

    if config.get("indexing", 1) and wb.get("indexing", {}).get("active"):
        ix = wb["indexing"]
        parts.append(f"INDEXING {ix.get('project', '?')} {ix.get('step', '')}")

    if config.get("sandbox", 1) and wb.get("sandbox", {}).get("active"):
        sb = wb["sandbox"]
        parts.append(f"SANDBOX {sb.get('project', '?')}:{sb.get('port', '?')}")

    if config.get("fossils", 1) and wb.get("fossils"):
        for f in wb["fossils"][:2]:
            parts.append(f"fossil {f.get('project', '?')} ({f.get('symbols', 0)} sym)")

    if config.get("shared_files", 1) and wb.get("shared_files"):
        parts.append(f"SHARED {len(wb['shared_files'])} files")

    if config.get("projects", 1) and wb.get("projects"):
        parts.append(f"{len(wb['projects'])} projects")

    if config.get("watchdog", 1) and wb.get("watchdog"):
        wd = wb["watchdog"]
        sshd = wd.get("sshd", {})
        status = sshd.get("status", "")
        if status == "recovered":
            parts.append(f"SSH recovered ({sshd.get('recoveries', 0)}x)")
        elif status and status != "ok":
            parts.append(f"SSH {status}")
        if wd.get("_stale"):
            parts.append("watchdog stale")

    if not parts:
        parts.append("workbench idle")

    return SEPARATOR.join(parts)


class TickerOverlay:
    """Transparent always-on-top scrolling ticker bar."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("NAI Ticker")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        # Load settings from API (or use defaults)
        self.settings = {
            "scroll_speed": 50, "opacity": 85, "bar_height": 28,
            "bg_color": "#1e1e2e", "text_color": "#cdd6f4",
            "position": "top", "poll_interval": 3,
        }
        self._load_settings()

        # Apply opacity
        try:
            self.root.attributes("-alpha", self.settings["opacity"] / 100.0)
        except tk.TclError:
            pass

        # Geometry: full screen width, bar_height tall
        screen_w = self.root.winfo_screenwidth()
        h = self.settings["bar_height"]
        y = 0 if self.settings["position"] == "top" else self.root.winfo_screenheight() - h
        self.root.geometry(f"{screen_w}x{h}+0+{y}")

        # Canvas for scrolling text
        bg = self.settings["bg_color"]
        self.canvas = tk.Canvas(
            self.root, width=screen_w, height=h,
            bg=bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Text item
        self.text_id = self.canvas.create_text(
            screen_w, h // 2,
            text="NAI Workbench starting...",
            fill=self.settings["text_color"],
            font=("Consolas", 11, "bold"),
            anchor="w",
        )

        # State
        self.ticker_text = "NAI Workbench starting..."
        self.scroll_x = float(screen_w)
        self.paused = False
        self._drag_y = None

        # Right-click menu
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Pause", command=self._toggle_pause)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self._quit)
        self.canvas.bind("<Button-3>", self._show_menu)

        # Dragging
        self.canvas.bind("<Button-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._do_drag)

        # Start polling thread
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        # Start animation
        self._animate()

    def _load_settings(self):
        """Load settings from API."""
        data = fetch_json("/api/ticker-settings")
        if data:
            for k in self.settings:
                if k in data:
                    val = data[k]
                    if k in ("scroll_speed", "opacity", "bar_height", "poll_interval"):
                        try:
                            self.settings[k] = int(val)
                        except (ValueError, TypeError):
                            pass
                    else:
                        self.settings[k] = str(val)

    def _poll_loop(self):
        """Background thread: polls workbench status + settings."""
        while self._running:
            try:
                wb = fetch_json("/api/workbench")
                config = fetch_json("/api/ticker-config")
                if wb and config:
                    self.ticker_text = build_ticker_text(wb, config)
                elif wb:
                    self.ticker_text = build_ticker_text(wb, {})

                # Reload settings periodically
                new_settings = fetch_json("/api/ticker-settings")
                if new_settings:
                    for k in self.settings:
                        if k in new_settings:
                            val = new_settings[k]
                            if k in ("scroll_speed", "opacity", "bar_height", "poll_interval"):
                                try:
                                    self.settings[k] = int(val)
                                except (ValueError, TypeError):
                                    pass
                            else:
                                self.settings[k] = str(val)
                    try:
                        self.root.attributes("-alpha", self.settings["opacity"] / 100.0)
                    except (tk.TclError, RuntimeError):
                        pass
            except Exception:
                pass
            time.sleep(self.settings.get("poll_interval", 3))

    def _animate(self):
        """Smooth scrolling animation loop (~60fps)."""
        if not self._running:
            return
        if not self.paused:
            speed = max(1, self.settings["scroll_speed"]) / 50.0
            self.scroll_x -= speed

            # Update text content
            self.canvas.itemconfig(self.text_id, text=self.ticker_text)
            self.canvas.coords(self.text_id, self.scroll_x, self.settings["bar_height"] // 2)

            # Reset position when text scrolls fully off-screen
            bbox = self.canvas.bbox(self.text_id)
            if bbox and bbox[2] < 0:
                self.scroll_x = float(self.root.winfo_screenwidth())

        self.root.after(16, self._animate)  # ~60fps

    def _show_menu(self, event):
        self.menu.entryconfig(0, label="Resume" if self.paused else "Pause")
        self.menu.tk_popup(event.x_root, event.y_root)

    def _toggle_pause(self):
        self.paused = not self.paused

    def _start_drag(self, event):
        self._drag_y = event.y_root - self.root.winfo_y()

    def _do_drag(self, event):
        if self._drag_y is not None:
            new_y = event.y_root - self._drag_y
            self.root.geometry(f"+0+{new_y}")

    def _quit(self):
        self._running = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    overlay = TickerOverlay()
    overlay.run()
