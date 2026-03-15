#!/usr/bin/env python3
"""Status ticker — scrolling terminal title bar with live workbench status.

Polls custodian DB + system state every 2 seconds and writes a compact
status string to the terminal title via ANSI escape sequences.
Flashes on completion events (fossil done, sandbox started, etc.).

Usage: Run as background job in any terminal session:
    python3 custodian/status_ticker.py &
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
SHARED_DIR = os.path.expanduser("~/.workbench/shared")
POLL_INTERVAL = 2  # seconds
FLASH_DURATION = 3  # seconds of flashing on events
FLASH_RATE = 0.3  # seconds between flash toggles

# Track state for change detection
_last_state = {}
_tty = None


def _get_tty():
    """Get a direct handle to the terminal, bypassing stdout redirection."""
    global _tty
    if _tty is None:
        try:
            _tty = open("/dev/tty", "w")
        except OSError:
            _tty = sys.stdout
    return _tty


def set_title(text):
    """Set terminal title via ANSI escape, writing directly to terminal."""
    tty = _get_tty()
    tty.write(f"\033]0;{text}\007")
    tty.flush()


def flash_title(text, duration=FLASH_DURATION):
    """Flash a message in the title bar by toggling visibility."""
    end = time.time() + duration
    bell_sent = False
    while time.time() < end:
        set_title(f">>> {text} <<<")
        if not bell_sent:
            tty = _get_tty()
            tty.write("\a")  # terminal bell — highlights tab
            tty.flush()
            bell_sent = True
        time.sleep(FLASH_RATE)
        set_title("")
        time.sleep(FLASH_RATE)
    # Final set so it stays visible
    set_title(f"  {text}  ")
    time.sleep(1)


def get_db():
    """Get read-only DB connection."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def check_indexing():
    """Check for running custodian indexing processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-af", "index_project.sh"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().split("\n") if "index_project.sh" in l]
            if lines:
                # Extract project name from command args
                for line in lines:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "index_project.sh" in p and i + 1 < len(parts):
                            project = parts[i + 1]
                            # Try to figure out which step
                            step = _guess_index_step(project)
                            return {"active": True, "project": project, "step": step}
        return {"active": False}
    except Exception:
        return {"active": False}


def _guess_index_step(project):
    """Guess the current indexing step by checking temp files."""
    temp = "/tmp/custodian"
    if os.path.isfile(f"{temp}/fossil-{project}.json"):
        sz = os.path.getsize(f"{temp}/fossil-{project}.json")
        if sz > 0:
            return "6/6 storing"
        return "5/6 sonnet"
    if os.path.isfile(f"{temp}/sonnet-input-{project}.txt"):
        return "5/6 sonnet"
    if os.path.isfile(f"{temp}/gitlog-{project}.txt"):
        return "4/6 prompt"
    if os.path.isfile(f"{temp}/symbols-{project}.json"):
        return "3/6 git"
    if os.path.isfile(f"{temp}/repomix-{project}.txt"):
        return "2/6 symbols"
    return "1/6 repomix"


def check_sandbox():
    """Check sandbox status from DB."""
    conn = get_db()
    if not conn:
        return {"active": False}
    try:
        row = conn.execute(
            """SELECT ab.container_name, ab.ports, ab.command, p.name
               FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
               WHERE ab.status = 'running'
               ORDER BY ab.started_at DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        if row:
            ports = json.loads(row["ports"]) if row["ports"] else {}
            port = list(ports.keys())[0] if ports else "?"
            return {
                "active": True,
                "project": row["name"],
                "port": port,
                "container": row["container_name"],
            }
        return {"active": False}
    except Exception:
        return {"active": False}


def check_agents():
    """Check for running agent jobs."""
    conn = get_db()
    if not conn:
        return {"active": False, "count": 0}
    try:
        rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM agent_runs WHERE status = 'running'"
        ).fetchone()
        conn.close()
        count = rows["cnt"] if rows else 0
        return {"active": count > 0, "count": count}
    except Exception:
        return {"active": False, "count": 0}


def check_shared_files():
    """Count files in shared folder."""
    if not os.path.isdir(SHARED_DIR):
        return 0
    try:
        return len([f for f in os.listdir(SHARED_DIR) if not f.startswith(".")])
    except Exception:
        return 0


def check_fossils():
    """Get latest fossil info for change detection."""
    conn = get_db()
    if not conn:
        return None
    try:
        row = conn.execute(
            """SELECT f.id, f.created_at, p.name
               FROM fossils f JOIN projects p ON p.id = f.project_id
               ORDER BY f.id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        if row:
            return {"id": row["id"], "project": row["name"], "created_at": row["created_at"]}
        return None
    except Exception:
        return None


def build_ticker_segments():
    """Build list of status segments to display."""
    global _last_state
    segments = []
    events = []  # completion events to flash

    # Indexing status
    idx = check_indexing()
    if idx["active"]:
        segments.append(f"indexing {idx['project']} {idx.get('step', '...')}")
    elif _last_state.get("indexing_active"):
        events.append(f"FOSSIL COMPLETE: {_last_state.get('indexing_project', '?')}")
    _last_state["indexing_active"] = idx["active"]
    _last_state["indexing_project"] = idx.get("project", "")

    # Sandbox status
    sb = check_sandbox()
    if sb["active"]:
        segments.append(f"sandbox {sb['project']}:{sb['port']}")
    elif _last_state.get("sandbox_active"):
        events.append("SANDBOX STOPPED")
    _last_state["sandbox_active"] = sb["active"]

    # Agent runs
    ag = check_agents()
    if ag["active"]:
        segments.append(f"agents: {ag['count']} running")

    # Shared files
    shared = check_shared_files()
    if shared > 0:
        segments.append(f"shared: {shared} files")

    # Latest fossil
    fossil = check_fossils()
    if fossil:
        # Detect new fossil
        last_fossil_id = _last_state.get("last_fossil_id")
        if last_fossil_id is not None and fossil["id"] != last_fossil_id:
            events.append(f"NEW FOSSIL: {fossil['project']}")
        _last_state["last_fossil_id"] = fossil["id"]

    # Default if nothing active
    if not segments:
        segments.append("idle")

    return segments, events


def run():
    """Main ticker loop."""
    # Ensure shared folder exists
    os.makedirs(SHARED_DIR, exist_ok=True)

    # Initialize state (don't flash on startup)
    _last_state["last_fossil_id"] = None
    fossil = check_fossils()
    if fossil:
        _last_state["last_fossil_id"] = fossil["id"]
    _last_state["indexing_active"] = check_indexing()["active"]
    _last_state["sandbox_active"] = check_sandbox()["active"]

    segment_idx = 0

    while True:
        try:
            segments, events = build_ticker_segments()

            # Flash completion events first
            for event in events:
                flash_title(event)

            # Rotate through segments
            if segments:
                seg = segments[segment_idx % len(segments)]
                # Build full title
                prefix = "\u2588"  # solid block as visual anchor
                title = f"{prefix} {seg}"

                # Add a subtle activity dot
                tick = int(time.time()) % 4
                dots = ["   ", ".  ", ".. ", "..."]
                title += f" {dots[tick]}"

                set_title(title)
                segment_idx += 1

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            set_title("")  # clear on exit
            break
        except Exception:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
