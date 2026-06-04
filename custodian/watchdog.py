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
BOX_TOOL_PORT_MIN = 9100
BOX_TOOL_PORT_MAX = 9199
BOX_TOOL_SERVER_CONTAINER_PATH = "/opt/box-tools/server.py"

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


# --- Project box reconciliation (every 2nd cycle) ---

def _pick_box_image(project_name, project_path, stack=""):
    """Match the runtime image selection for best-effort watchdog provisioning."""
    devcontainer_path = os.path.join(project_path, ".devcontainer")
    if os.path.isdir(devcontainer_path):
        dockerfile = os.path.join(devcontainer_path, "Dockerfile")
        if os.path.isfile(dockerfile):
            image_name = f"alpha-{project_name}:latest"
            subprocess.run(
                ["docker", "build", "-t", image_name, "-f", dockerfile, project_path],
                capture_output=True,
                text=True,
                timeout=300,
            )
            return image_name

    check = subprocess.run(
        ["docker", "image", "inspect", "nai-sandbox:latest"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check.returncode == 0:
        return "nai-sandbox:latest"


def _allocate_tool_server_port(conn, project_id):
    existing = conn.execute(
        "SELECT tool_server_port FROM project_boxes WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if existing and existing[0]:
        return int(existing[0])

    used = {
        int(row[0])
        for row in conn.execute(
            "SELECT tool_server_port FROM project_boxes WHERE tool_server_port IS NOT NULL"
        ).fetchall()
    }
    for port in range(BOX_TOOL_PORT_MIN, BOX_TOOL_PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("No available tool server ports in range 9100-9199")


def _copy_and_start_box_tool_server(container_name, port):
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "box_tool_server.py")
    subprocess.run(
        ["docker", "exec", container_name, "mkdir", "-p", "/opt/box-tools"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    subprocess.run(
        ["docker", "cp", source_path, f"{container_name}:{BOX_TOOL_SERVER_CONTAINER_PATH}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    has_tools = subprocess.run(
        ["docker", "exec", container_name, "test", "-d", "/workspace/tools"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if has_tools.returncode != 0:
        return

    subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", f"pkill -f '{BOX_TOOL_SERVER_CONTAINER_PATH}' >/dev/null 2>&1 || true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    subprocess.run(
        [
            "docker", "exec", "-d",
            "-e", f"BOX_TOOL_PORT={int(port)}",
            container_name,
            "python3", BOX_TOOL_SERVER_CONTAINER_PATH,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    if "Python" in stack:
        return "python:3.12"
    if any(s in stack for s in ("Node", "React", "Next", "Electron")):
        return "node:22"
    return "python:3.12"


def _provision_project_box(conn, project_row):
    """Ensure a project has a persistent Docker box and DB row."""
    project_path = project_row["path"]
    if not os.path.isdir(project_path):
        return False

    container_name = f"alpha-{project_row['name']}"
    tool_server_port = _allocate_tool_server_port(conn, project_row["id"])
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}|{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if inspect.returncode == 0:
        image_name, running = (inspect.stdout.strip().split("|", 1) + [""])[:2]
        if running.strip().lower() != "true":
            start = subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if start.returncode != 0:
                raise RuntimeError((start.stderr or start.stdout or "docker start failed").strip())
    else:
        image_name = _pick_box_image(project_row["name"], project_path, project_row["stack"] or "")
        run = subprocess.run(
            [
                "docker", "run", "-d", "--network", "host", "--name", container_name,
                "-v", f"{project_path}:/workspace", "-w", "/workspace",
                "--restart", "unless-stopped",
                image_name, "sleep", "infinity",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if run.returncode != 0:
            raise RuntimeError((run.stderr or run.stdout or "docker run failed").strip())

    _copy_and_start_box_tool_server(container_name, tool_server_port)

    conn.execute(
        """
        INSERT INTO project_boxes (
            project_id, container_name, image, status, env_vars, ports, tool_server_port,
            restart_policy, error_message, created_at, updated_at
        )
        VALUES (?, ?, ?, 'running', '{}', '{}', ?, 'unless-stopped', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(project_id) DO UPDATE SET
            container_name = excluded.container_name,
            image = excluded.image,
            status = 'running',
            tool_server_port = excluded.tool_server_port,
            restart_policy = excluded.restart_policy,
            error_message = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (project_row["id"], container_name, image_name, tool_server_port),
    )
    conn.commit()
    return True


def reconcile_boxes():
    """Keep project boxes aligned with Docker and enforce always-on runtimes."""
    global _sandbox_corrections
    corrected = 0

    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM project_boxes"
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

            if row["status"] == "running" and not alive:
                conn.execute(
                    "UPDATE project_boxes SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                corrected += 1
                _log(f"Marked stopped project box: {container} (box_id={row['id']})")
            elif row["status"] == "stopped" and alive:
                conn.execute(
                    "UPDATE project_boxes SET status = 'running', error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                corrected += 1
                _log(f"Adopted already-running project box: {container} (box_id={row['id']})")
            elif row["status"] == "stopped" and not alive:
                start = subprocess.run(
                    ["docker", "start", container],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if start.returncode == 0:
                    _copy_and_start_box_tool_server(container, row["tool_server_port"] or _allocate_tool_server_port(conn, row["project_id"]))
                    conn.execute(
                        "UPDATE project_boxes SET status = 'running', error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (row["id"],),
                    )
                    conn.commit()
                    corrected += 1
                    _log(f"Restarted project box: {container} (box_id={row['id']})")

        projects = conn.execute(
            "SELECT id, name, path, stack FROM projects WHERE status = 'active'"
        ).fetchall()
        for project in projects:
            existing = conn.execute(
                "SELECT id FROM project_boxes WHERE project_id = ?",
                (project["id"],),
            ).fetchone()
            if existing:
                continue
            try:
                if _provision_project_box(conn, project):
                    corrected += 1
                    _log(f"Provisioned missing project box: alpha-{project['name']}")
            except Exception as e:
                _log(f"Project box provision error for {project['name']}: {e}")

        conn.close()
    except Exception as e:
        _log(f"Project box reconcile error: {e}")

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

            # Project boxes: every 2nd cycle
            if cycle % 2 == 0:
                reconcile_boxes()

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
