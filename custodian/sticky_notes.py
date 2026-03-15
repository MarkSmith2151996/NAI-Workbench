#!/usr/bin/env python3
"""Sticky Notes — minimal TUI widget. Stick it anywhere in Wave."""

import os
import sqlite3
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Static, Button

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")

COLORS = ["yellow", "green", "blue", "pink", "purple"]
DOTS = {"yellow": "#eab308", "green": "#22c55e", "blue": "#3b82f6", "pink": "#ec4899", "purple": "#a855f7"}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def time_ago(dt_str):
    try:
        created = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - created
        mins = int(diff.total_seconds() / 60)
        if mins < 1:
            return "now"
        if mins < 60:
            return f"{mins}m"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h"
        return f"{hrs // 24}d"
    except Exception:
        return ""


class StickyNotesApp(App):
    TITLE = "Notes"
    CSS = """
    Screen { background: #1e1e2e; }
    #input-row { height: 3; background: #181825; padding: 0 1; }
    #note-input { width: 1fr; }
    #add-btn { min-width: 6; }
    #notes { padding: 0 1; }
    .note-row { height: auto; padding: 0; margin: 0; }
    .note-text { width: 1fr; height: auto; padding: 0; }
    .note-x { min-width: 3; height: 1; margin: 0; padding: 0; }
    #status { height: 1; dock: bottom; background: #181825; padding: 0 1; color: #6c7086; }
    """

    BINDINGS = [
        Binding("a", "focus_input", "Add"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "cycle_color", "Color"),
        Binding("q", "quit", "Quit"),
    ]

    color_idx = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="input-row"):
            yield Input(placeholder="new note...", id="note-input")
            yield Button("+", variant="warning", id="add-btn")
        yield VerticalScroll(id="notes")
        yield Static("", id="status")

    def on_mount(self):
        self._refresh()

    def _refresh(self):
        notes_container = self.query_one("#notes", VerticalScroll)
        notes_container.remove_children()

        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM sticky_notes ORDER BY pinned DESC, done ASC, created_at DESC"
        ).fetchall()
        conn.close()

        if not rows:
            notes_container.mount(Static("[dim]no notes yet[/dim]"))
        else:
            for r in rows:
                accent = DOTS.get(r["color"], "#eab308")
                text = r["text"].replace("[", "\\[")
                pin = " *" if r["pinned"] else ""
                age = time_ago(r["created_at"])

                if r["done"]:
                    line = f"[dim {accent}]x[/] [strike dim]{text}[/strike dim] [dim]{age}[/]"
                else:
                    line = f"[bold {accent}]>[/] {text}{pin} [dim]{age}[/]"

                row = Horizontal(classes="note-row")
                notes_container.mount(row)
                row.mount(Static(line, classes="note-text"))
                row.mount(Button("x", id=f"del-{r['id']}", classes="note-x", variant="error"))
                row.mount(Button("v" if not r["done"] else "u", id=f"done-{r['id']}", classes="note-x"))
                row.mount(Button("^" if not r["pinned"] else "v", id=f"pin-{r['id']}", classes="note-x"))

        color = COLORS[self.color_idx]
        accent = DOTS[color]
        active = sum(1 for r in rows if not r["done"])
        self.query_one("#status", Static).update(
            f" {active}/{len(rows)} | [{accent}]{color}[/] | a=add c=color r=refresh q=quit"
        )

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "note-input":
            self._add_note()

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if bid == "add-btn":
            self._add_note()
        elif bid.startswith("del-"):
            nid = int(bid[4:])
            conn = get_db()
            conn.execute("DELETE FROM sticky_notes WHERE id=?", (nid,))
            conn.commit()
            conn.close()
            self._refresh()
        elif bid.startswith("done-"):
            nid = int(bid[5:])
            conn = get_db()
            conn.execute("UPDATE sticky_notes SET done = 1 - done WHERE id=?", (nid,))
            conn.commit()
            conn.close()
            self._refresh()
        elif bid.startswith("pin-"):
            nid = int(bid[4:])
            conn = get_db()
            conn.execute("UPDATE sticky_notes SET pinned = 1 - pinned WHERE id=?", (nid,))
            conn.commit()
            conn.close()
            self._refresh()

    def _add_note(self):
        inp = self.query_one("#note-input", Input)
        text = inp.value.strip()
        if not text:
            return
        conn = get_db()
        conn.execute("INSERT INTO sticky_notes (text, color) VALUES (?, ?)", (text, COLORS[self.color_idx]))
        conn.commit()
        conn.close()
        inp.value = ""
        self._refresh()

    def action_focus_input(self):
        self.query_one("#note-input", Input).focus()

    def action_refresh(self):
        self._refresh()

    def action_cycle_color(self):
        self.color_idx = (self.color_idx + 1) % len(COLORS)
        self._refresh()


if __name__ == "__main__":
    StickyNotesApp().run()
