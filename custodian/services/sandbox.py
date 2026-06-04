from __future__ import annotations

import collections
import json
import os
import platform as _platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import quote

from custodian.db.connection import DB_PATH, db_connection
from mcp.types import TextContent

LIVE_FILE_TREE_ENTRY_LIMIT = 50
SHARED_FOLDER_ROOT = "/mnt/c/Users/Big A/custodian-shared"
SHARED_FOLDER_MAX_BYTES = 5 * 1024 * 1024
SHARED_FOLDER_BINARY_SNIFF_BYTES = 8192
SHARED_FOLDER_OVERSIZE_PREVIEW_LINES = 100
SHARED_FOLDER_CATEGORY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _check_wsl():
    if _platform.system() != "Linux" or not os.path.exists("/proc/version"):
        return False
    with open("/proc/version") as f:
        return "microsoft" in f.read().lower()


_IS_WSL = _check_wsl()
CUSTODIAN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _to_native_path(path):
    if not _IS_WSL or not path:
        return path
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def _find_symbol(*args, **kwargs):
    try:
        from custodian.parse_symbols import find_symbol
    except ImportError:
        from parse_symbols import find_symbol
    return find_symbol(*args, **kwargs)


def get_project_by_name(conn, name):
    row = conn.execute("SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'", (f"%{name}%",)).fetchone()

_sandbox_log = collections.deque(maxlen=5000)
_sandbox_log_lock = threading.Lock()
_sandbox_project = None
_sandbox_container = None
_sandbox_command = None
_sandbox_port = None
_sandbox_state_lock = threading.Lock()
_log_reader_stop = threading.Event()
_background_procs = []
_background_procs_lock = threading.Lock()
ROUTER_PORT = 7777

def _start_router_fallback():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", ROUTER_PORT))
        # Port already bound — standalone service is running
        return
    except (ConnectionRefusedError, OSError):
        pass
    try:
        from sandbox_router import run_router as _run_sandbox_router
        threading.Thread(target=_run_sandbox_router, daemon=True).start()
    except ImportError:
        pass

def _docker_log_reader(container_name, stop_event):
    """Background thread: streams docker logs into the ring buffer.

    Checks *stop_event* between lines so the thread exits promptly when a new
    reader is started or the server shuts down.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "200", container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        with _background_procs_lock:
            _background_procs.append(proc)
        import select as _sel
        while not stop_event.is_set():
            # Use select to avoid blocking forever on readline
            ready, _, _ = _sel.select([proc.stdout], [], [], 1.0)
            if not ready:
                continue
            raw_line = proc.stdout.readline()
            if not raw_line:
                break  # EOF — container stopped
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            with _sandbox_log_lock:
                _sandbox_log.append(decoded)
    except Exception as e:
        if not stop_event.is_set():
            print(f"[custodian] docker log reader died: {e}", file=sys.stderr)
    finally:
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            with _background_procs_lock:
                try:
                    _background_procs.remove(proc)
                except ValueError:
                    pass

def _detect_sandbox_command(project_path):
    """Auto-detect the dev command for a project.

    Returns (command, port, app_type) where app_type is 'web' or 'terminal'.
    """
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

    # Use python3 on Linux/WSL, python on Windows
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
                if any(kw in content for kw in ["tkinter", "Tkinter", "pygame", "PyQt5",
                        "PyQt6", "PySide2", "PySide6", "wx", "gi.repository", "kivy",
                        "pyglet", "turtle"]):
                    return f"{py} {entry}", 8080, "gui"
                if any(kw in content for kw in ["flask", "Flask", "fastapi", "FastAPI", "uvicorn"]):
                    return f"{py} {entry}", 5000, "web"
            except OSError:
                pass
            return f"{py} {entry}", 5000, "web"

    return None, None, None

def _detect_test_command(project_path):
    """Auto-detect the test command for a project."""
    pkg = os.path.join(project_path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg) as f:
                data = json.load(f)
            scripts = data.get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except (json.JSONDecodeError, OSError):
            pass

    if os.path.isfile(os.path.join(project_path, "pytest.ini")) or os.path.isfile(
        os.path.join(project_path, "pyproject.toml")
    ):
        return "pytest"

    if os.path.isdir(os.path.join(project_path, "tests")):
        return "pytest"

    return None

def _pick_image(project_name, project_path, stack=""):
    """Decide the Docker image based on devcontainer or stack detection.

    Prefers nai-sandbox:latest (pre-built with ttyd/tmux/noVNC/Node/etc.)
    when available; falls back to stock images otherwise.
    """
    # Custom devcontainer always wins
    devcontainer_path = os.path.join(project_path, ".devcontainer")
    if os.path.isdir(devcontainer_path):
        dockerfile = os.path.join(devcontainer_path, "Dockerfile")
        if os.path.isfile(dockerfile):
            image_name = f"alpha-{project_name}:latest"
            subprocess.run(
                ["docker", "build", "-t", image_name, "-f", dockerfile, project_path],
                capture_output=True, text=True, timeout=300,
            )
            return image_name

    # Prefer pre-built sandbox image (has ttyd, tmux, noVNC, Node, etc.)
    check = subprocess.run(
        ["docker", "image", "inspect", "nai-sandbox:latest"],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode == 0:
        return "nai-sandbox:latest"

    # Fallback to stock images
    if "Python" in stack:
        return "python:3.12"
    elif any(s in stack for s in ("Node", "React", "Next", "Electron")):
        return "node:22"
    return "python:3.12"

def _is_gui_command(command):
    """Check if a command needs an X display (desktop GUI app)."""
    return any(hint in command for hint in GUI_HINTS)

def _is_web_command(command):
    """Check if a command is a web server (serves HTTP natively)."""
    return any(hint in command for hint in WEB_HINTS)

def _get_or_create_container(project_name, project_path, stack="", port=None):
    """Get existing alpha build container, or create one.

    If *port* is given and the existing container doesn't map that port,
    the container is recreated with the port exposed.
    """
    container_name = f"alpha-{project_name}"
    shared_project_path = _ensure_shared_project_root(project_name)

    # Check if container exists
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True,
    )
    container_exists = result.returncode == 0

    # If port requested, verify the existing container has it mapped
    if container_exists and port:
        port_check = subprocess.run(
            ["docker", "port", container_name, str(port)],
            capture_output=True, text=True,
        )
        if port_check.returncode != 0 or not port_check.stdout.strip():
            # Port not mapped — must recreate
            subprocess.run(["docker", "rm", "-f", container_name],
                           capture_output=True, text=True, timeout=30)
            container_exists = False

    if container_exists and not _container_has_bind_mount(container_name, "/workspace/shared", shared_project_path):
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True, text=True, timeout=30)
        container_exists = False

    if container_exists:
        is_running = result.stdout.strip() == "true"
        if not is_running:
            subprocess.run(["docker", "start", container_name], capture_output=True)
        return container_name

    # Container doesn't exist — create one
    image_name = _pick_image(project_name, project_path, stack)

    run_cmd = [
        "docker", "run", "-d", "--network", "host", "--name", container_name,
        "-v", f"{project_path}:/workspace", "-w", "/workspace",
        "-v", f"{shared_project_path}:/workspace/shared",
    ]
    run_cmd += [image_name, "sleep", "infinity"]

    subprocess.run(run_cmd, capture_output=True, text=True, timeout=60)

    # For stock images (not nai-sandbox), install ttyd + tmux at runtime
    if "nai-sandbox" not in image_name:
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-c",
             "which ttyd >/dev/null 2>&1 && ttyd --version >/dev/null 2>&1 || "
             "(curl -sL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 "
             "-o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd)"],
            capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-c",
             "which tmux >/dev/null 2>&1 || "
             "(apt-get update -qq && apt-get install -y -qq tmux >/dev/null 2>&1)"],
            capture_output=True, text=True, timeout=60,
        )

    # Save to alpha_builds table
    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if project:
            cid_result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", container_name],
                capture_output=True, text=True,
            )
            cid = cid_result.stdout.strip()[:12] if cid_result.returncode == 0 else ""
            ports_json = json.dumps({str(port): str(port)}) if port else "{}"
            conn.execute(
                "DELETE FROM alpha_builds WHERE project_id = ?", (project["id"],)
            )
            conn.execute(
                """INSERT INTO alpha_builds (project_id, container_id, container_name,
                   image, status, ports, started_at) VALUES (?, ?, ?, ?, 'running', ?, datetime('now'))""",
                (project["id"], cid, container_name, image_name, ports_json),
            )
            conn.commit()

    return container_name

def _auto_install_deps(container_name, project_path):
    """Auto-install dependencies from requirements.txt or package.json if present.

    Runs silently — skips if deps are already satisfied.  Called by
    handle_sandbox_start before launching the user command.

    Note: tmux/ttyd are pre-installed in nai-sandbox:latest.  For stock
    images the runtime install in _get_or_create_container handles them.
    """
    try:
        req_txt = os.path.join(project_path, "requirements.txt")
        pkg_json = os.path.join(project_path, "package.json")

        if os.path.isfile(req_txt):
            # pip install -r requirements.txt (quiet, skips already-installed)
            print(f"[sandbox] Auto-installing pip deps for {container_name}…",
                  file=sys.stderr)
            subprocess.run(
                ["docker", "exec", "-w", "/workspace", container_name,
                 "bash", "-c", "pip install -q -r requirements.txt 2>/dev/null"],
                capture_output=True, text=True, timeout=120,
            )
        elif os.path.isfile(pkg_json):
            print(f"[sandbox] Auto-installing npm deps for {container_name}…",
                  file=sys.stderr)
            subprocess.run(
                ["docker", "exec", "-w", "/workspace", container_name,
                 "bash", "-c", "npm install --silent 2>/dev/null"],
                capture_output=True, text=True, timeout=120,
            )
    except Exception as e:
        # Never fail the sandbox start over a dep install issue
        print(f"[sandbox] Auto-install warning: {e}", file=sys.stderr)

def _install_novnc_stack(container_name):
    """Install Xvfb + x11vnc + noVNC + fluxbox on demand. Skips if already present.

    With nai-sandbox:latest this is a no-op (everything pre-installed).
    For stock images, installs the full GUI stack at runtime.
    """
    check = subprocess.run(
        ["docker", "exec", container_name, "which", "Xvfb"],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode == 0:
        return  # Already installed (nai-sandbox or previous run)
    subprocess.run(
        ["docker", "exec", "-u", "root", container_name, "bash", "-c",
         "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
         "apt-get install -y --no-install-recommends "
         "xvfb x11vnc novnc fluxbox x11-xserver-utils xterm python3-tk "
         "&& rm -rf /var/lib/apt/lists/* "
         "&& echo '<html><head><meta http-equiv=\"refresh\" "
         "content=\"0;url=sandbox.html\">"
         "</head></html>' > /usr/share/novnc/index.html"],
        capture_output=True, text=True, timeout=300,
    )
    # Deploy custom fullscreen VNC viewer
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as tf:
        tf.write(SANDBOX_VNC_HTML)
        tf_path = tf.name
    try:
        subprocess.run(
            ["docker", "cp", tf_path, f"{container_name}:/usr/share/novnc/sandbox.html"],
            capture_output=True, text=True, timeout=10,
        )
    finally:
        os.unlink(tf_path)

async def handle_sandbox_start(args):
    """Start a sandbox: ensure Docker container exists and run command inside it.

    Three display modes (auto-detected):
    - Web: command serves HTTP natively (flask, uvicorn, etc.) → direct
    - GUI: command needs X display (tkinter, pygame, Qt) → Xvfb + noVNC wrap
    - Terminal: everything else (TUIs, REPLs, scripts) → ttyd wrap
    All modes serve HTTP on the mapped port for the sandbox iframe.
    """
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container

    project_name = args["project"]
    command_override = args.get("command")
    port_override = args.get("port")

    log_query("sandbox_start", project_name, args)

    # Sweep stale sandbox entries: verify all "running" rows against Docker
    try:
        with db_connection() as conn:
            stale_rows = conn.execute(
                "SELECT id, container_name FROM alpha_builds WHERE status = 'running'"
            ).fetchall()
            for row in stale_rows:
                try:
                    check = subprocess.run(
                        ["docker", "inspect", "-f", "{{.State.Running}}", row["container_name"]],
                        capture_output=True, text=True, timeout=5,
                    )
                    alive = check.returncode == 0 and check.stdout.strip() == "true"
                except Exception:
                    alive = False
                if not alive:
                    conn.execute(
                        "UPDATE alpha_builds SET status = 'stopped' WHERE id = ?",
                        (row["id"],),
                    )
            conn.commit()
    except Exception:
        pass  # Don't block start on cleanup failure

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    # Determine command
    detected_app_type = None
    if command_override:
        command = command_override
        port = port_override
        # Smart default: if command looks like a web server and no port given, use 8080
        if not port and command:
            if _is_web_command(command):
                port = 8080
    else:
        command, port, detected_app_type = _detect_sandbox_command(project_path)
        if not command:
            return [TextContent(
                type="text",
                text=f"Could not auto-detect dev command for '{project_name}'. "
                     "Pass a 'command' argument (e.g., 'npm run dev').",
            )]

    # --- Display mode detection ---
    # Three modes: web (direct HTTP), gui (noVNC desktop), terminal (ttyd)
    # Rules:
    #   - If user explicitly passed a port → assume web server, don't wrap
    #   - If command matches web hints → web, don't wrap
    #   - If already wrapped (ttyd/novnc-wrap) → don't wrap (restart recovery)
    #   - If command matches GUI hints → noVNC desktop wrap
    #   - Otherwise → ttyd terminal wrap
    original_command = command
    is_ttyd_wrapped = command.startswith("ttyd ")
    is_novnc_wrapped = command.startswith("novnc-wrap ")
    user_gave_port = port_override is not None
    display_mode = "web"  # default

    is_web = user_gave_port or _is_web_command(command) or detected_app_type == "web"
    if is_web or is_ttyd_wrapped or is_novnc_wrapped:
        # Already handled or explicitly web — keep as-is
        if is_ttyd_wrapped:
            display_mode = "terminal"
        elif is_novnc_wrapped:
            display_mode = "gui"
    elif _is_gui_command(command) or detected_app_type == "gui":
        # Desktop GUI app — wrap with noVNC
        if not port:
            port = 8080
        display_mode = "gui"
        # Actual wrapping happens after container creation (need to install stack first)
    else:
        # Terminal app — wrap with tmux + ttyd
        # The app runs inside a tmux session ("sandbox"), and ttyd attaches to
        # that session.  This way multiple viewers (PC + laptop) share the SAME
        # terminal — no duplicate processes, no rendering glitches.
        # Output is also tee'd to /tmp/sandbox.log so sandbox_logs can read it.
        if not port:
            port = 8080
        import shlex
        # Build the tmux launch + ttyd attach command:
        #   1. Kill any stale tmux session
        #   2. Start tmux with the real command (+ tee to log)
        #   3. ttyd attaches to that tmux session (shared view)
        tmux_inner = f'stty cols 120 rows 40 2>/dev/null; {command} 2>&1 | tee -a /tmp/sandbox.log'
        tmux_setup = (
            f"tmux kill-session -t sandbox 2>/dev/null; "
            f"tmux new-session -d -s sandbox -x 120 -y 40 {shlex.quote(tmux_inner)}; "
            f"sleep 0.5"
        )
        # ttyd runs tmux attach — every browser tab shares one session
        command = (
            f"bash -c {shlex.quote(tmux_setup)} && "
            f"ttyd -p {port} -W -t fontSize=14 -t disableReconnect=true "
            f"tmux attach -t sandbox"
        )
        is_ttyd_wrapped = True
        display_mode = "terminal"

    try:
        # Get or create the Docker container (with port mapping if needed)
        container_name = _get_or_create_container(
            project_name, project_path, (project["stack"] or ""), port=port
        )

        # --- Auto-install dependencies if present ---
        _auto_install_deps(container_name, project_path)

        # --- noVNC setup for GUI apps ---
        if display_mode == "gui":
            _install_novnc_stack(container_name)
            # For stock images (no pre-baked novnc-wrap), deploy the wrapper script
            wrap_check = subprocess.run(
                ["docker", "exec", container_name, "test", "-x", "/usr/local/bin/novnc-wrap"],
                capture_output=True, text=True, timeout=5,
            )
            if wrap_check.returncode != 0:
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
                    tf.write(NOVNC_WRAPPER)
                    tf_path = tf.name
                try:
                    subprocess.run(
                        ["docker", "cp", tf_path, f"{container_name}:/usr/local/bin/novnc-wrap"],
                        capture_output=True, text=True, timeout=10,
                    )
                    subprocess.run(
                        ["docker", "exec", "-u", "root", container_name,
                         "chmod", "+x", "/usr/local/bin/novnc-wrap"],
                        capture_output=True, text=True, timeout=10,
                    )
                finally:
                    os.unlink(tf_path)
            if not is_novnc_wrapped:
                command = f"novnc-wrap {port} {command}"
                is_novnc_wrapped = True

        # Stop any existing log reader before starting a new one
        _log_reader_stop.set()

        _sandbox_log.clear()
        with _sandbox_state_lock:
            _sandbox_project = project_name
            _sandbox_command = command
            _sandbox_port = port
            _sandbox_container = container_name

        # Run the command inside the container (detached via docker exec + nohup)
        subprocess.run(
            ["docker", "exec", "-d", "-w", "/workspace", container_name,
             "bash", "-c",
             f"rm -f /tmp/sandbox.log; nohup {command} > /tmp/sandbox_wrapper.log 2>&1 &"],
            capture_output=True, text=True, timeout=10,
        )

        # Update alpha_builds with command, port info, display_mode
        with db_connection() as conn2:
            ports_json = json.dumps({str(port): str(port)}) if port else "{}"
            conn2.execute(
                """UPDATE alpha_builds SET command=?, ports=?, display_mode=?,
                   status='running', started_at=datetime('now')
                   WHERE container_name=?""",
                (command, ports_json, display_mode, container_name),
            )
            conn2.commit()

        # Start log reader with a fresh stop event
        _log_reader_stop.clear()
        threading.Thread(
            target=_docker_log_reader, args=(container_name, _log_reader_stop),
            daemon=True,
        ).start()

        port_info = f" on port {port}" if port else ""
        mode_note = ""
        if display_mode == "terminal":
            mode_note = " (via ttyd terminal)"
        elif display_mode == "gui":
            mode_note = " (via noVNC desktop)"
        return [TextContent(
            type="text",
            text=f"Started `{original_command}`{port_info}{mode_note} in container {container_name} for {project_name}.",
        )]

    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start sandbox: {e}")]

async def handle_sandbox_stop(args):
    """Stop the sandbox container."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container

    log_query("sandbox_stop")

    # Stop log reader thread
    _log_reader_stop.set()

    with _sandbox_state_lock:
        container = _sandbox_container
        name = _sandbox_project or "unknown"

    # If in-memory state lost (MCP restart), recover from DB
    if not container:
        with db_connection() as conn:
            row = conn.execute(
                """SELECT ab.container_name, p.name as project_name
                   FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                   WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
            ).fetchone()
        if row:
            container = row["container_name"]
            name = row["project_name"]
        else:
            return [TextContent(type="text", text="No sandbox is running.")]

    try:
        subprocess.run(
            ["docker", "stop", container],
            capture_output=True, text=True, timeout=30,
        )

        # Update alpha_builds DB
        with db_connection() as conn:
            conn.execute(
                "UPDATE alpha_builds SET status='stopped', stopped_at=datetime('now') WHERE container_name=?",
                (container,),
            )
            conn.commit()

        with _sandbox_state_lock:
            _sandbox_project = None
            _sandbox_command = None
            _sandbox_port = None
            _sandbox_container = None

        return [TextContent(type="text", text=f"Stopped sandbox for {name}.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error stopping sandbox: {e}")]

async def handle_sandbox_restart(args):
    """Restart the sandbox container."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container
    log_query("sandbox_restart")

    # Recover from DB if in-memory state lost (MCP restart)
    with _sandbox_state_lock:
        if not _sandbox_project or not _sandbox_command:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.container_name, ab.command, ab.ports, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                _sandbox_container = row["container_name"]
                _sandbox_project = row["project_name"]
                _sandbox_command = row["command"]
                ports = json.loads(row["ports"]) if row["ports"] else {}
                _sandbox_port = int(list(ports.keys())[0]) if ports else None
            else:
                return [TextContent(type="text", text="No sandbox to restart. Use sandbox_start first.")]

        project_name = _sandbox_project
        command = _sandbox_command
        port = _sandbox_port

    await handle_sandbox_stop({})
    start_args = {"project": project_name, "command": command}
    if port:
        start_args["port"] = port
    return await handle_sandbox_start(start_args)

async def handle_sandbox_status(args):
    """Get sandbox container status."""
    global _sandbox_project, _sandbox_command, _sandbox_port, _sandbox_container

    # Recover from DB if in-memory state lost (MCP restart)
    with _sandbox_state_lock:
        if not _sandbox_container:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.container_name, ab.command, ab.ports, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                _sandbox_container = row["container_name"]
                _sandbox_project = row["project_name"]
                _sandbox_command = row["command"]
                ports = json.loads(row["ports"]) if row["ports"] else {}
                _sandbox_port = int(list(ports.keys())[0]) if ports else None
            else:
                return [TextContent(type="text", text=json.dumps({
                    "status": "stopped",
                    "project": None,
                    "command": None,
                    "container": None,
                }))]

        container = _sandbox_container
        project = _sandbox_project
        command = _sandbox_command
        port = _sandbox_port

    # Check if container is running (outside the lock — slow operation)
    check = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    is_running = check.returncode == 0 and check.stdout.strip() == "true"

    with _sandbox_log_lock:
        error_count = sum(
            1 for line in _sandbox_log
            if "error" in line.lower() and "warning" not in line.lower()
        )
        warning_count = sum(1 for line in _sandbox_log if "warning" in line.lower())
        log_lines = len(_sandbox_log)

    # If container is dead, correct the DB and clear in-memory state
    if not is_running:
        with db_connection() as conn:
            conn.execute(
                "UPDATE alpha_builds SET status = 'stopped' WHERE container_name = ? AND status = 'running'",
                (container,),
            )
            conn.commit()
        with _sandbox_state_lock:
            _sandbox_project = None
            _sandbox_container = None
            _sandbox_command = None
            _sandbox_port = None

    result = {
        "status": "running" if is_running else "stopped",
        "project": project,
        "command": command,
        "container": container,
        "port": port,
        "log_lines": log_lines,
        "errors": error_count,
        "warnings": warning_count,
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_sandbox_logs(args):
    """Get logs from the sandbox container."""
    lines_count = args.get("lines", 50)
    log_filter = args.get("filter")

    with _sandbox_state_lock:
        sb_project = _sandbox_project
        sb_container = _sandbox_container

    # Read from /tmp/sandbox.log inside the container (where nohup writes)
    if sb_container:
        try:
            result = subprocess.run(
                ["docker", "exec", sb_container,
                 "tail", "-n", str(lines_count), "/tmp/sandbox.log"],
                capture_output=True, text=True, timeout=10,
            )
            all_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            # Also grab docker logs as fallback
            if not all_lines or all_lines == [""]:
                result2 = subprocess.run(
                    ["docker", "logs", "--tail", str(lines_count), sb_container],
                    capture_output=True, text=True, timeout=10,
                )
                all_lines = (result2.stdout + result2.stderr).strip().split("\n")
        except Exception:
            with _sandbox_log_lock:
                all_lines = list(_sandbox_log)
    else:
        with _sandbox_log_lock:
            all_lines = list(_sandbox_log)

    if log_filter == "error":
        all_lines = [l for l in all_lines if "error" in l.lower()]
    elif log_filter == "warning":
        all_lines = [l for l in all_lines if "warning" in l.lower()]

    tail = all_lines[-lines_count:]

    result = {
        "project": sb_project,
        "container": sb_container,
        "total_lines": len(all_lines),
        "showing": len(tail),
        "filter": log_filter,
        "output": "\n".join(tail),
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_sandbox_test(args):
    """Run tests inside the sandbox container."""
    global _sandbox_project, _sandbox_container, _sandbox_command, _sandbox_port
    command_override = args.get("command")

    with _sandbox_state_lock:
        sb_project = _sandbox_project
        sb_container = _sandbox_container

    # Recover state from DB if globals lost (MCP restart)
    if not sb_container:
        try:
            with db_connection() as conn:
                row = conn.execute(
                    """SELECT ab.*, p.name as project_name
                       FROM alpha_builds ab JOIN projects p ON p.id = ab.project_id
                       WHERE ab.status = 'running' ORDER BY ab.started_at DESC LIMIT 1"""
                ).fetchone()
            if row:
                with _sandbox_state_lock:
                    _sandbox_container = row["container_name"]
                    _sandbox_project = row["project_name"]
                    _sandbox_command = row["command"] or ""
                    ports = json.loads(row["ports"]) if row["ports"] else {}
                    _sandbox_port = int(list(ports.keys())[0]) if ports else None
                    sb_container = _sandbox_container
                    sb_project = _sandbox_project
        except Exception:
            pass

    if not sb_container:
        return [TextContent(
            type="text",
            text="No sandbox running. Use sandbox_start first.",
        )]

    if command_override:
        command = command_override
    else:
        # Auto-detect test command by checking files inside container
        project_path = None
        if sb_project:
            with db_connection() as conn:
                project = get_project_by_name(conn, sb_project)
            if project:
                project_path = _to_native_path(project["path"])

        if project_path:
            command = _detect_test_command(project_path)
        else:
            command = None

        if not command:
            return [TextContent(
                type="text",
                text="Could not auto-detect test command. Pass a 'command' argument.",
            )]

    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", sb_container,
             "bash", "-c", command],
            capture_output=True, text=True, timeout=120,
        )

        output = {
            "command": command,
            "container": sb_container,
            "exit_code": result.returncode,
            "passed": result.returncode == 0,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Test command timed out after 120 seconds.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to run tests: {e}")]

async def handle_sandbox_install(args):
    """Install dependencies inside the sandbox container."""
    project_name = args["project"]
    packages = args.get("packages", [])
    manager = args.get("manager")

    log_query("sandbox_install", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])

    # Ensure container exists
    container_name = _get_or_create_container(
        project_name, project_path, (project["stack"] or "")
    )

    # Auto-detect package manager
    if not manager:
        if os.path.isfile(os.path.join(project_path, "package.json")):
            manager = "npm"
        else:
            manager = "pip"

    try:
        if packages:
            # Sanitize package names to prevent shell injection
            import shlex
            safe_pkgs = " ".join(shlex.quote(p) for p in packages)
            if manager == "pip":
                install_cmd = f"pip install {safe_pkgs}"
            else:
                install_cmd = f"npm install {safe_pkgs}"
        else:
            if manager == "pip":
                install_cmd = "pip install -r requirements.txt"
            else:
                install_cmd = "npm install"

        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name,
             "bash", "-c", install_cmd],
            capture_output=True, text=True, timeout=180,
        )

        output = {
            "project": project_name,
            "container": container_name,
            "manager": manager,
            "packages": packages if packages else "from manifest",
            "exit_code": result.returncode,
            "success": result.returncode == 0,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Install timed out after 180 seconds.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to install: {e}")]

async def handle_sandbox_exec(args):
    """Run a command in the sandbox container and return output directly.

    Unlike sandbox_test (which requires a running sandbox), this just needs
    the Docker container to exist.  Perfect for diagnosing crashes, checking
    files, or running one-off commands.
    """
    project_name = args["project"]
    command = args["command"]
    timeout_s = min(args.get("timeout", 30), 120)

    log_query("sandbox_exec", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    container_name = f"alpha-{project_name}"

    # Ensure container is running
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        if "true" not in inspect.stdout.lower():
            subprocess.run(
                ["docker", "start", container_name],
                capture_output=True, text=True, timeout=15,
            )
    except Exception:
        # Container might not exist — try to create it
        _get_or_create_container(
            project_name, project_path, (project["stack"] or "")
        )

    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name,
             "bash", "-c", command],
            capture_output=True, text=True, timeout=timeout_s,
        )

        output = {
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]

    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text=f"Command timed out after {timeout_s}s.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to exec: {e}")]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def start(**params):
    return _unwrap(await handle_sandbox_start(params))


async def stop(**params):
    return _unwrap(await handle_sandbox_stop(params))


async def restart(**params):
    return _unwrap(await handle_sandbox_restart(params))


async def status(**params):
    return _unwrap(await handle_sandbox_status(params))


async def logs(**params):
    return _unwrap(await handle_sandbox_logs(params))


async def test(**params):
    return _unwrap(await handle_sandbox_test(params))


async def install(**params):
    return _unwrap(await handle_sandbox_install(params))


async def exec_command(**params):
    return _unwrap(await handle_sandbox_exec(params))
