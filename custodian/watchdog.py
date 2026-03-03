#!/usr/bin/env python3
"""Workbench Watchdog — monitors and auto-recovers critical services.

Lightweight daemon that runs on 10s cycles, checking:
- sshd alive + /run/sshd exists  (every cycle)
- Docker running                 (every 3rd cycle)
- WSL IP stability               (every 6th cycle)
- Stale sandbox DB entries        (every 2nd cycle)

Writes /tmp/watchdog-health.json atomically on every cycle.

Usage:
    python3 custodian/watchdog.py          # foreground
    systemctl --user start workbench-watchdog  # via systemd

Install: bash bin/install-watchdog
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
HEALTH_FILE = "/tmp/watchdog-health.json"
CYCLE_INTERVAL = 10  # seconds

# Recovery counters (reset on process restart)
_sshd_recoveries = 0
_docker_recoveries = 0
_sandbox_corrections = 0
_start_time = time.time()
_running = True


def _log(msg):
    """Print with timestamp for journalctl."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --- sshd check (every cycle) ---

def check_sshd():
    """Check if sshd is running and /run/sshd exists. Recover if not."""
    global _sshd_recoveries

    pid = None
    status = "ok"

    # Check /run/sshd directory
    run_sshd_exists = os.path.isdir("/run/sshd")

    # Check if sshd process is alive
    try:
        result = subprocess.run(
            ["pgrep", "-x", "sshd"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            pid = int(pids[0])
    except Exception:
        pass

    if pid and run_sshd_exists:
        return {"status": "ok", "pid": pid, "recoveries": _sshd_recoveries}

    # Need recovery
    status = "recovering"
    _log(f"sshd needs recovery (pid={pid}, /run/sshd={run_sshd_exists})")

    try:
        if not run_sshd_exists:
            subprocess.run(
                ["sudo", "mkdir", "-p", "/run/sshd"],
                capture_output=True, text=True, timeout=5,
            )
            _log("Created /run/sshd")

        if not pid:
            subprocess.run(
                ["sudo", "/usr/sbin/sshd"],
                capture_output=True, text=True, timeout=5,
            )
            _log("Started sshd")

        # Verify recovery
        time.sleep(0.5)
        result = subprocess.run(
            ["pgrep", "-x", "sshd"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split("\n")[0])
            _sshd_recoveries += 1
            _log(f"sshd recovered (pid={pid}, total recoveries={_sshd_recoveries})")
            return {"status": "recovered", "pid": pid, "recoveries": _sshd_recoveries}
        else:
            _log("sshd recovery FAILED")
            return {"status": "failed", "pid": None, "recoveries": _sshd_recoveries}
    except Exception as e:
        _log(f"sshd recovery error: {e}")
        return {"status": "error", "pid": None, "recoveries": _sshd_recoveries}


# --- Docker check (every 3rd cycle) ---

def check_docker():
    """Check if Docker daemon is running."""
    global _docker_recoveries

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return {"status": "ok", "recoveries": _docker_recoveries}
    except Exception:
        pass

    # Try recovery
    _log("Docker not responding, attempting restart...")
    try:
        subprocess.run(
            ["sudo", "systemctl", "start", "docker"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(2)
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            _docker_recoveries += 1
            _log(f"Docker recovered (total={_docker_recoveries})")
            return {"status": "recovered", "recoveries": _docker_recoveries}
    except Exception as e:
        _log(f"Docker recovery error: {e}")

    return {"status": "down", "recoveries": _docker_recoveries}


# --- WSL IP check (every 6th cycle) ---

def check_wsl_ip():
    """Detect current WSL IP. Log-only — can't run netsh from WSL."""
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ips = result.stdout.strip().split()
            # Filter to the 172.x.x.x NAT IP
            wsl_ip = next((ip for ip in ips if ip.startswith("172.")), ips[0] if ips else "unknown")
            return wsl_ip
    except Exception:
        pass
    return "unknown"


# --- Stale sandbox cleanup (every 2nd cycle) ---

def check_stale_sandboxes():
    """Find alpha_builds marked 'running' where the container is dead."""
    global _sandbox_corrections
    corrected = 0

    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, container_name FROM alpha_builds WHERE status = 'running'"
        ).fetchall()

        for row in rows:
            container = row["container_name"]
            try:
                result = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container],
                    capture_output=True, text=True, timeout=5,
                )
                alive = result.returncode == 0 and result.stdout.strip() == "true"
            except Exception:
                alive = False

            if not alive:
                conn.execute(
                    "UPDATE alpha_builds SET status = 'stopped' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                corrected += 1
                _log(f"Marked stale sandbox stopped: {container} (build_id={row['id']})")

        conn.close()
    except Exception as e:
        _log(f"Stale sandbox check error: {e}")

    _sandbox_corrections += corrected
    return corrected


# --- Health file writer ---

def write_health(sshd, docker, wsl_ip):
    """Write health status atomically to /tmp/watchdog-health.json."""
    health = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sshd": sshd,
        "docker": docker,
        "wsl_ip": wsl_ip,
        "sandbox_corrections": _sandbox_corrections,
        "uptime_seconds": int(time.time() - _start_time),
    }

    # Atomic write: write to temp file, then rename
    try:
        fd, tmp_path = tempfile.mkstemp(dir="/tmp", prefix="watchdog-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(health, f, indent=2)
        shutil.move(tmp_path, HEALTH_FILE)
    except Exception as e:
        _log(f"Failed to write health file: {e}")


# --- Signal handling ---

def _handle_signal(signum, frame):
    global _running
    _log(f"Received signal {signum}, shutting down")
    _running = False


# --- Main loop ---

def run():
    global _running

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _log("Workbench watchdog started")
    _log(f"DB: {DB_PATH}")
    _log(f"Health file: {HEALTH_FILE}")

    cycle = 0
    wsl_ip = "unknown"
    docker_status = {"status": "unknown", "recoveries": 0}

    while _running:
        try:
            # sshd: every cycle
            sshd_status = check_sshd()

            # Docker: every 3rd cycle
            if cycle % 3 == 0:
                docker_status = check_docker()

            # WSL IP: every 6th cycle
            if cycle % 6 == 0:
                wsl_ip = check_wsl_ip()

            # Stale sandboxes: every 2nd cycle
            if cycle % 2 == 0:
                check_stale_sandboxes()

            # Write health
            write_health(sshd_status, docker_status, wsl_ip)

            cycle += 1
            time.sleep(CYCLE_INTERVAL)

        except Exception as e:
            _log(f"Cycle error: {e}")
            time.sleep(CYCLE_INTERVAL)

    _log("Watchdog stopped")


if __name__ == "__main__":
    run()
