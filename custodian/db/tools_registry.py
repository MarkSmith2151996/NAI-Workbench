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
from custodian.db.projects import _ensure_shared_project_root, _safe_json_loads
from custodian.db.system import log_query
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

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BOX_TOOL_PORT_MIN = 9100
BOX_TOOL_PORT_MAX = 9199
BOX_TOOL_SERVER_CONTAINER_PATH = "/opt/box-tools/server.py"

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

def _inspect_container(container_name):
    """Return Docker state for a named container without raising."""
    try:
        result = subprocess.run(
            [
                "docker", "inspect", "--format",
                "{{.Id}}|{{.Config.Image}}|{{.State.Running}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {"exists": False, "error": str(exc)}

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "").strip()
        return {"exists": False, "error": error_text}

    container_id, image, running = (result.stdout.strip().split("|", 2) + ["", "", ""])[:3]
    return {
        "exists": True,
        "container_id": container_id[:12],
        "image": image,
        "running": running.strip().lower() == "true",
        "error": None,
    }

def _container_has_bind_mount(container_name, destination, source=None):
    """Return True when a container has a bind mount at the expected path."""
    try:
        result = subprocess.run(
            [
                "docker", "inspect", "--format",
                "{{range .Mounts}}{{println .Source \"|\" .Destination}}{{end}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False

    if result.returncode != 0:
        return False

    expected_destination = os.path.normpath(destination)
    expected_source = os.path.normpath(source) if source else None
    for raw_line in result.stdout.splitlines():
        parts = [part.strip() for part in raw_line.split("|", 1)]
        if len(parts) != 2:
            continue
        mount_source, mount_destination = parts
        if os.path.normpath(mount_destination) != expected_destination:
            continue
        if expected_source and os.path.normpath(mount_source) != expected_source:
            continue
        return True

    return False

def _upsert_project_box_row(
    conn,
    project_id,
    container_name,
    image,
    status,
    env_vars=None,
    ports=None,
    tool_server_port=None,
    restart_policy="unless-stopped",
    last_healthcheck=None,
    error_message=None,
):
    """Insert or update the persistent runtime record for a project."""
    conn.execute(
        """
        INSERT INTO project_boxes (
            project_id, container_name, image, status, env_vars, ports, tool_server_port,
            restart_policy, last_healthcheck, error_message, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(project_id) DO UPDATE SET
            container_name = excluded.container_name,
            image = excluded.image,
            status = excluded.status,
            env_vars = excluded.env_vars,
            ports = excluded.ports,
            tool_server_port = excluded.tool_server_port,
            restart_policy = excluded.restart_policy,
            last_healthcheck = excluded.last_healthcheck,
            error_message = excluded.error_message,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            project_id,
            container_name,
            image,
            status,
            json.dumps(env_vars or {}),
            json.dumps(ports or {}),
            tool_server_port,
            restart_policy,
            last_healthcheck,
            error_message,
        ),
    )

def _allocate_tool_server_port(conn, project_id):
    existing = conn.execute(
        "SELECT tool_server_port FROM project_boxes WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if existing and existing["tool_server_port"]:
        return int(existing["tool_server_port"])

    used_ports = {
        int(row[0])
        for row in conn.execute(
            "SELECT tool_server_port FROM project_boxes WHERE tool_server_port IS NOT NULL"
        ).fetchall()
    }
    for port in range(BOX_TOOL_PORT_MIN, BOX_TOOL_PORT_MAX + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("No available tool server ports in range 9100-9199")

def _box_tool_server_request(port, method, path, payload=None, timeout=5):
    url = f"http://127.0.0.1:{int(port)}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body else {"error": str(exc)}
        except json.JSONDecodeError:
            payload = {"error": body or str(exc)}
        return exc.code, payload
    except URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc

def _box_tool_server_request_in_container(container_name, port, method, path, payload=None, timeout=5):
    script = """
import json
import sys
import urllib.error
import urllib.request

method, url, raw = sys.argv[1], sys.argv[2], sys.argv[3]
data = raw.encode('utf-8') if raw else None
headers = {'Content-Type': 'application/json'} if raw else {}
req = urllib.request.Request(url, data=data, method=method, headers=headers)

try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode('utf-8')
        out = {'status': resp.status, 'payload': json.loads(body) if body else None}
except urllib.error.HTTPError as exc:
    body = exc.read().decode('utf-8', errors='replace')
    try:
        payload = json.loads(body) if body else {'error': str(exc)}
    except json.JSONDecodeError:
        payload = {'error': body or str(exc)}
    out = {'status': exc.code, 'payload': payload}

print(json.dumps(out))
"""
    raw_payload = json.dumps(payload) if payload is not None else ""
    result = subprocess.run(
        [
            "docker", "exec", container_name,
            "python3", "-c", script,
            method.upper(), f"http://127.0.0.1:{int(port)}{path}", raw_payload,
        ],
        capture_output=True,
        text=True,
        timeout=timeout + 3,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "in-container tool request failed").strip())
    response = json.loads(result.stdout.strip() or "{}")
    return response.get("status", 500), response.get("payload")

def _request_box_tool_server(container_name, port, method, path, payload=None, timeout=5):
    try:
        return _box_tool_server_request(port, method, path, payload=payload, timeout=timeout)
    except RuntimeError:
        return _box_tool_server_request_in_container(
            container_name,
            port,
            method,
            path,
            payload=payload,
            timeout=timeout,
        )

def _box_tool_server_healthy(port):
    try:
        status, payload = _box_tool_server_request(port, "GET", "/health", timeout=2)
        return status == 200 and isinstance(payload, dict) and payload.get("status") == "ok"
    except Exception:
        return False

def _box_tool_server_healthy_in_container(container_name, port):
    try:
        status, payload = _box_tool_server_request_in_container(container_name, port, "GET", "/health", timeout=2)
        return status == 200 and isinstance(payload, dict) and payload.get("status") == "ok"
    except Exception:
        return False

def _copy_box_tool_server(container_name):
    source_path = os.path.join(CUSTODIAN_ROOT, "box_tool_server.py")
    mkdir_result = subprocess.run(
        ["docker", "exec", container_name, "mkdir", "-p", "/opt/box-tools"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if mkdir_result.returncode != 0:
        raise RuntimeError((mkdir_result.stderr or mkdir_result.stdout or "mkdir failed").strip())

    copy_result = subprocess.run(
        ["docker", "cp", source_path, f"{container_name}:{BOX_TOOL_SERVER_CONTAINER_PATH}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if copy_result.returncode != 0:
        raise RuntimeError((copy_result.stderr or copy_result.stdout or "docker cp failed").strip())

def _box_has_tools_dir(container_name):
    result = subprocess.run(
        ["docker", "exec", container_name, "test", "-d", "/workspace/tools"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0

def _restart_box_tool_server(container_name, port):
    _copy_box_tool_server(container_name)
    if not _box_has_tools_dir(container_name):
        return False

    subprocess.run(
        ["docker", "exec", container_name, "sh", "-c", f"pkill -f '{BOX_TOOL_SERVER_CONTAINER_PATH}' >/dev/null 2>&1 || true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    start_result = subprocess.run(
        [
            "docker", "exec", "-d",
            "-e", f"BOX_TOOL_PORT={int(port)}",
            container_name,
            "python3", BOX_TOOL_SERVER_CONTAINER_PATH,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if start_result.returncode != 0:
        raise RuntimeError((start_result.stderr or start_result.stdout or "tool server start failed").strip())

    for _ in range(20):
        if _box_tool_server_healthy(port) or _box_tool_server_healthy_in_container(container_name, port):
            return True
        time.sleep(0.25)
    raise RuntimeError(f"Tool server failed to start on port {port}")

def _ensure_box_tool_server(project, box_row):
    port = box_row["tool_server_port"]
    if not port:
        with db_connection() as conn:
            port = _allocate_tool_server_port(conn, project["id"])
            _upsert_project_box_row(
                conn,
                project_id=project["id"],
                container_name=box_row["container_name"],
                image=box_row["image"],
                status=box_row["status"],
                env_vars=_safe_json_loads(box_row["env_vars"], {}),
                ports=_safe_json_loads(box_row["ports"], {}),
                tool_server_port=port,
                restart_policy=box_row["restart_policy"] or "unless-stopped",
                last_healthcheck=box_row["last_healthcheck"],
                error_message=box_row["error_message"],
            )
            conn.commit()

    if not _box_has_tools_dir(box_row["container_name"]):
        raise RuntimeError(f"project {project['name']} has no tool server running: /workspace/tools not found")

    if not _box_tool_server_healthy(port):
        _restart_box_tool_server(box_row["container_name"], port)

    return int(port)

def _provision_project_box(project_name, project_path, stack="", env_vars=None):
    """Ensure the persistent Docker runtime exists for a project."""
    native_project_path = _to_native_path(project_path)
    shared_project_path = _ensure_shared_project_root(project_name)
    container_name = f"alpha-{project_name}"
    env_vars = env_vars or {}

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found.")
        tool_server_port = _allocate_tool_server_port(conn, project["id"])

    inspect = _inspect_container(container_name)
    image_name = inspect.get("image") or _pick_image(project_name, native_project_path, stack)
    if inspect["exists"] and not _container_has_bind_mount(container_name, "/workspace/shared", shared_project_path):
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        inspect = {"exists": False, "running": False, "image": image_name}

    try:
        if inspect["exists"] and inspect["running"]:
            status = "running"
        elif inspect["exists"]:
            start_result = subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if start_result.returncode != 0:
                raise RuntimeError((start_result.stderr or start_result.stdout or "docker start failed").strip())
            inspect = _inspect_container(container_name)
            image_name = inspect.get("image") or image_name
            status = "running"
        else:
            run_cmd = [
                "docker", "run", "-d", "--network", "host", "--name", container_name,
                "-v", f"{native_project_path}:/workspace", "-w", "/workspace",
                "-v", f"{shared_project_path}:/workspace/shared",
                "--restart", "unless-stopped",
            ]
            for key, value in sorted(env_vars.items()):
                run_cmd += ["-e", f"{key}={value}"]
            run_cmd += [image_name, "sleep", "infinity"]

            run_result = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if run_result.returncode != 0:
                raise RuntimeError((run_result.stderr or run_result.stdout or "docker run failed").strip())
            inspect = _inspect_container(container_name)
            image_name = inspect.get("image") or image_name
            status = "running"

        try:
            _auto_install_deps(container_name, native_project_path)
        except Exception as exc:
            print(f"[project-box] Dependency install warning for {project_name}: {exc}", file=sys.stderr)

        if _box_has_tools_dir(container_name):
            _restart_box_tool_server(container_name, tool_server_port)
        else:
            _copy_box_tool_server(container_name)

        with db_connection() as conn:
            _upsert_project_box_row(
                conn,
                project_id=project["id"],
                container_name=container_name,
                image=image_name,
                status=status,
                env_vars=env_vars,
                ports={},
                tool_server_port=tool_server_port,
                restart_policy="unless-stopped",
                error_message=None,
            )
            conn.commit()

        return container_name
    except Exception as exc:
        with db_connection() as conn:
            _upsert_project_box_row(
                conn,
                project_id=project["id"],
                container_name=container_name,
                image=image_name,
                status="error",
                env_vars=env_vars,
                ports={},
                tool_server_port=tool_server_port,
                restart_policy="unless-stopped",
                error_message=str(exc),
            )
            conn.commit()
        raise

def _ensure_project_box(project):
    """Lazy-provision and return the current project_boxes row."""
    with db_connection() as conn:
        box = conn.execute(
            "SELECT * FROM project_boxes WHERE project_id = ?",
            (project["id"],),
        ).fetchone()

    if box:
        return box

    _provision_project_box(project["name"], project["path"], project["stack"] or "")
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM project_boxes WHERE project_id = ?",
            (project["id"],),
        ).fetchone()

def _ensure_box_running(project, box_row):
    """Start a project box if needed and keep DB state aligned."""
    container_name = box_row["container_name"] if box_row else f"alpha-{project['name']}"
    inspect = _inspect_container(container_name)
    shared_project_path = _ensure_shared_project_root(project["name"])

    if not inspect["exists"]:
        _provision_project_box(project["name"], project["path"], project["stack"] or "")
        inspect = _inspect_container(container_name)
    elif not _container_has_bind_mount(container_name, "/workspace/shared", shared_project_path):
        env_vars = _safe_json_loads(box_row["env_vars"], {}) if box_row else {}
        _provision_project_box(project["name"], project["path"], project["stack"] or "", env_vars)
        inspect = _inspect_container(container_name)
    elif not inspect["running"]:
        start_result = subprocess.run(
            ["docker", "start", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if start_result.returncode != 0:
            raise RuntimeError((start_result.stderr or start_result.stdout or "docker start failed").strip())
        inspect = _inspect_container(container_name)

    if not inspect["exists"] or not inspect["running"]:
        raise RuntimeError(f"Project box '{container_name}' is not running.")

    with db_connection() as conn:
        tool_server_port = _allocate_tool_server_port(conn, project["id"])

    if _box_has_tools_dir(container_name):
        _restart_box_tool_server(container_name, tool_server_port)
    else:
        _copy_box_tool_server(container_name)

    with db_connection() as conn:
        _upsert_project_box_row(
            conn,
            project_id=project["id"],
            container_name=container_name,
            image=inspect.get("image") or (box_row["image"] if box_row else _pick_image(project["name"], _to_native_path(project["path"]), project["stack"] or "")),
            status="running",
            env_vars=_safe_json_loads(box_row["env_vars"], {}) if box_row else {},
            ports=_safe_json_loads(box_row["ports"], {}) if box_row else {},
            tool_server_port=tool_server_port,
            restart_policy=(box_row["restart_policy"] if box_row and box_row["restart_policy"] else "unless-stopped"),
            last_healthcheck=box_row["last_healthcheck"] if box_row else None,
            error_message=None,
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM project_boxes WHERE project_id = ?",
            (project["id"],),
        ).fetchone()

def _sync_box_row(conn, project, box_row):
    """Verify a box against Docker and repair stale DB state."""
    inspect = _inspect_container(box_row["container_name"])
    status = box_row["status"]
    error_message = box_row["error_message"]

    if inspect["exists"] and inspect["running"]:
        status = "running"
        error_message = None
    elif inspect["exists"]:
        status = "stopped"
    elif status == "running":
        status = "stopped"

    if status != box_row["status"] or error_message != box_row["error_message"]:
        _upsert_project_box_row(
            conn,
            project_id=project["id"],
            container_name=box_row["container_name"],
            image=inspect.get("image") or box_row["image"],
            status=status,
            env_vars=_safe_json_loads(box_row["env_vars"], {}),
            ports=_safe_json_loads(box_row["ports"], {}),
            tool_server_port=box_row["tool_server_port"],
            restart_policy=box_row["restart_policy"] or "unless-stopped",
            last_healthcheck=box_row["last_healthcheck"],
            error_message=error_message,
        )
        conn.commit()
        box_row = conn.execute(
            "SELECT * FROM project_boxes WHERE project_id = ?",
            (project["id"],),
        ).fetchone()

    return box_row, inspect

def _normalize_tool_schema(value, field_name):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"'{field_name}' must be an object.")
    return value

def _validate_tool_handler_code(handler_code):
    code = str(handler_code or "")
    if "def handle(" not in code:
        raise ValueError("handler_code must define a 'def handle(params)' function")
    return code

def _tool_record_dict(row):
    data = dict(row)
    for key in ("input_schema", "output_schema"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except json.JSONDecodeError:
                pass
    return data

def _write_tool_file_to_box(container_name, handler_path, handler_code):
    handler_dir = os.path.dirname(handler_path)
    mkdir_result = subprocess.run(
        ["docker", "exec", container_name, "mkdir", "-p", handler_dir],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if mkdir_result.returncode != 0:
        raise RuntimeError((mkdir_result.stderr or mkdir_result.stdout or "mkdir failed").strip())

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(handler_code)
        temp_path = handle.name
    try:
        copy_result = subprocess.run(
            ["docker", "cp", temp_path, f"{container_name}:{handler_path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if copy_result.returncode != 0:
            raise RuntimeError((copy_result.stderr or copy_result.stdout or "docker cp failed").strip())
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

def _verify_box_tool_module(container_name, tool_name, handler_path):
    verify = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "python3",
            "-c",
            (
                "import importlib.util; "
                f"spec = importlib.util.spec_from_file_location({tool_name!r}, {handler_path!r}); "
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
                "assert hasattr(mod, 'handle'), 'No handle function'; print('OK')"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if verify.returncode != 0 or "OK" not in verify.stdout:
        raise ValueError(f"Handler validation failed: {(verify.stderr or verify.stdout).strip()}")

def _reload_tool_server(container_name, port):
    if not port:
        return False
    try:
        status, _payload = _request_box_tool_server(container_name, port, "POST", "/reload", payload={})
        return status == 200
    except Exception:
        return False

def _upsert_tool_registry_row(
    conn,
    *,
    tool_name,
    project,
    description,
    input_schema,
    output_schema,
    handler_code,
    wrapper_path,
    existing=None,
):
    current_version = int(existing["version"] or 0) if existing and "version" in existing.keys() else 0
    new_version = current_version + 1
    conn.execute(
        """
        INSERT OR REPLACE INTO tool_registry (
            id, tool_name, project, description, source_module, source_class, source_method,
            hook_point, return_type, known_side_effects, wrapper_path,
            input_schema, output_schema, handler_code, version, status,
            created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), datetime('now'))
        """,
        (
            existing["id"] if existing else None,
            tool_name,
            project,
            description,
            wrapper_path,
            None,
            None,
            "handle(params)",
            "dict",
            None,
            wrapper_path,
            json.dumps(input_schema) if input_schema else None,
            json.dumps(output_schema) if output_schema else None,
            handler_code,
            new_version,
            "active",
            "create_tool",
            existing["created_at"] if existing else None,
        ),
    )
    row = conn.execute(
        "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
        (tool_name, project),
    ).fetchone()
    return row, new_version

async def handle_register_tool(args):
    tool_name = str(args.get("tool_name") or "").strip()
    project = str(args.get("project") or "").strip()
    source_module = str(args.get("source_module") or "").strip()
    source_class = str(args.get("source_class") or "").strip() or None
    source_method = str(args.get("source_method") or "").strip() or None
    hook_point = str(args.get("hook_point") or "").strip()
    return_type = str(args.get("return_type") or "").strip()
    known_side_effects = str(args.get("known_side_effects") or "").strip() or None
    wrapper_path = str(args.get("wrapper_path") or "").strip()
    created_by = str(args.get("created_by") or "manual").strip() or "manual"

    log_query("register_tool", project or None, args)

    required = {
        "tool_name": tool_name,
        "project": project,
        "source_module": source_module,
        "hook_point": hook_point,
        "return_type": return_type,
        "wrapper_path": wrapper_path,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return [TextContent(type="text", text=f"Error: missing required field(s): {', '.join(missing)}")]

    with db_connection() as conn:
        existing = conn.execute(
            "SELECT id, created_at FROM tool_registry WHERE tool_name = ? AND project = ?",
            (tool_name, project),
        ).fetchone()
        conn.execute(
            """
            INSERT OR REPLACE INTO tool_registry (
                id, tool_name, project, source_module, source_class, source_method,
                hook_point, return_type, known_side_effects, wrapper_path,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), datetime('now'))
            """,
            (
                existing["id"] if existing else None,
                tool_name,
                project,
                source_module,
                source_class,
                source_method,
                hook_point,
                return_type,
                known_side_effects,
                wrapper_path,
                created_by,
                existing["created_at"] if existing else None,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
            (tool_name, project),
        ).fetchone()
        conn.commit()

    return [TextContent(type="text", text=json.dumps(dict(row), indent=2))]

async def handle_get_tool_registry(args):
    project = str(args.get("project") or "").strip()

    with db_connection() as conn:
        if project:
            rows = conn.execute(
                """
                SELECT * FROM tool_registry
                WHERE project = ?
                ORDER BY project, tool_name
                """,
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM tool_registry
                ORDER BY project, tool_name
                """
            ).fetchall()

    result = [dict(row) for row in rows]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_tool(args):
    tool_name = str(args.get("name") or "").strip()
    project = str(args.get("project") or "").strip()

    if not tool_name or not project:
        return [TextContent(type="text", text="Error: 'name' and 'project' are required.")]

    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
            (tool_name, project),
        ).fetchone()
    if not row:
        return [TextContent(type="text", text=f"Error: tool '{tool_name}' not found for project '{project}'.")]
    return [TextContent(type="text", text=json.dumps(_tool_record_dict(row), indent=2))]

async def handle_create_tool(args):
    tool_name = str(args.get("name") or "").strip()
    project_name = str(args.get("project") or "").strip()
    description = str(args.get("description") or "").strip()
    log_query("create_tool", project_name or None, {**args, "handler_code": "<omitted>"})

    if not tool_name or not project_name:
        return [TextContent(type="text", text="Error: 'name' and 'project' are required.")]

    try:
        input_schema = _normalize_tool_schema(args.get("input_schema"), "input_schema")
        output_schema = _normalize_tool_schema(args.get("output_schema"), "output_schema")
        handler_code = _validate_tool_handler_code(args.get("handler_code"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]
        existing = conn.execute(
            "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
            (tool_name, project["name"]),
        ).fetchone()
        if existing and existing["status"] != "deleted":
            return [TextContent(type="text", text=f"Error: tool '{tool_name}' already exists for project '{project['name']}'. Use update_tool instead.")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        port = _ensure_box_tool_server(project, box)
        handler_path = f"/workspace/tools/{tool_name}.py"
        wrapper_path = f"tools/{tool_name}.py"
        _write_tool_file_to_box(box["container_name"], handler_path, handler_code)
        _verify_box_tool_module(box["container_name"], tool_name, handler_path)
        with db_connection() as conn:
            row, version = _upsert_tool_registry_row(
                conn,
                tool_name=tool_name,
                project=project["name"],
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                handler_code=handler_code,
                wrapper_path=wrapper_path,
                existing=existing,
            )
            conn.commit()
        reloaded = _reload_tool_server(box["container_name"], port)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    payload = {
        "name": tool_name,
        "project": project["name"],
        "description": description,
        "handler_path": handler_path,
        "version": version,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "status": "created",
        "reloaded": reloaded,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]

async def handle_update_tool(args):
    tool_name = str(args.get("name") or "").strip()
    project_name = str(args.get("project") or "").strip()
    log_query("update_tool", project_name or None, {**args, "handler_code": "<omitted>"} if "handler_code" in args else args)

    if not tool_name or not project_name:
        return [TextContent(type="text", text="Error: 'name' and 'project' are required.")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]
        existing = conn.execute(
            "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
            (tool_name, project["name"]),
        ).fetchone()
        if not existing:
            return [TextContent(type="text", text=f"Error: tool '{tool_name}' not found for project '{project['name']}'.")]

    try:
        description = str(args.get("description") if "description" in args else existing["description"] or "").strip()
        input_schema = _normalize_tool_schema(args.get("input_schema") if "input_schema" in args else json.loads(existing["input_schema"]) if existing["input_schema"] else {}, "input_schema")
        output_schema = _normalize_tool_schema(args.get("output_schema") if "output_schema" in args else json.loads(existing["output_schema"]) if existing["output_schema"] else {}, "output_schema")
        handler_code = _validate_tool_handler_code(args.get("handler_code") if "handler_code" in args else existing["handler_code"])
    except (ValueError, json.JSONDecodeError) as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        port = _ensure_box_tool_server(project, box)
        handler_path = f"/workspace/tools/{tool_name}.py"
        wrapper_path = existing["wrapper_path"] or f"tools/{tool_name}.py"
        _write_tool_file_to_box(box["container_name"], handler_path, handler_code)
        _verify_box_tool_module(box["container_name"], tool_name, handler_path)
        with db_connection() as conn:
            row, version = _upsert_tool_registry_row(
                conn,
                tool_name=tool_name,
                project=project["name"],
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                handler_code=handler_code,
                wrapper_path=wrapper_path,
                existing=existing,
            )
            conn.commit()
        reloaded = _reload_tool_server(box["container_name"], port)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    payload = {
        "name": tool_name,
        "project": project["name"],
        "description": description,
        "handler_path": handler_path,
        "version": version,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "status": "updated",
        "reloaded": reloaded,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]

async def handle_box_status(args):
    project_name = str(args.get("project") or "").strip()

    with db_connection() as conn:
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Project '{project_name}' not found.")]
            rows = conn.execute(
                "SELECT pb.*, p.name as project_name FROM project_boxes pb JOIN projects p ON p.id = pb.project_id WHERE pb.project_id = ?",
                (project["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT pb.*, p.name as project_name, p.path, p.stack, p.status as project_status FROM project_boxes pb JOIN projects p ON p.id = pb.project_id ORDER BY p.name"
            ).fetchall()

        boxes = []
        for row in rows:
            project_row = {
                "id": row["project_id"],
                "name": row["project_name"],
                "path": row["path"] if "path" in row.keys() else None,
                "stack": row["stack"] if "stack" in row.keys() else None,
            }
            synced_row, inspect = _sync_box_row(conn, project_row, row)
            boxes.append({
                "project": row["project_name"],
                "container": synced_row["container_name"],
                "image": inspect.get("image") or synced_row["image"],
                "status": synced_row["status"],
                "tool_server_port": synced_row["tool_server_port"],
                "last_healthcheck": synced_row["last_healthcheck"],
                "created_at": synced_row["created_at"],
                "error_message": synced_row["error_message"],
            })

    if project_name:
        payload = boxes[0] if boxes else None
    else:
        payload = {
            "summary": {
                "total": len(boxes),
                "running": sum(1 for box in boxes if box["status"] == "running"),
                "stopped": sum(1 for box in boxes if box["status"] == "stopped"),
                "error": sum(1 for box in boxes if box["status"] == "error"),
            },
            "boxes": boxes,
        }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]

async def handle_box_logs(args):
    project_name = args["project"]
    lines_count = max(1, int(args.get("lines", 50)))
    log_filter = args.get("filter")

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]
        box = conn.execute(
            "SELECT * FROM project_boxes WHERE project_id = ?",
            (project["id"],),
        ).fetchone()

    if not box:
        return [TextContent(type="text", text=f"No box found for '{project_name}'.")]

    container_name = box["container_name"]
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "tail", "-n", str(lines_count), "/tmp/sandbox.log"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        content = result.stdout
        if result.returncode != 0 or not content.strip():
            fallback = subprocess.run(
                ["docker", "logs", "--tail", str(lines_count), container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            content = (fallback.stdout + fallback.stderr).strip()

        lines = content.splitlines()
        if log_filter == "error":
            lines = [line for line in lines if "error" in line.lower()]
        elif log_filter == "warning":
            lines = [line for line in lines if "warning" in line.lower()]

        payload = {
            "project": project["name"],
            "container": container_name,
            "lines": len(lines),
            "content": "\n".join(lines[-lines_count:]),
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Failed to read box logs: {exc}")]

async def handle_list_project_tools(args):
    project_name = args["project"]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        port = _ensure_box_tool_server(project, box)
        _, payload = _request_box_tool_server(box["container_name"], port, "GET", "/tools")
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Failed to list project tools: {exc}")]

async def handle_call_project_tool(args):
    project_name = args["project"]
    tool_name = args["tool_name"]
    params = args.get("params", {})

    log_query("call_project_tool", project_name, args)

    if not isinstance(params, dict):
        return [TextContent(type="text", text="Failed to call project tool: params must be an object")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        port = _ensure_box_tool_server(project, box)
        _, payload = _request_box_tool_server(
            box["container_name"],
            port,
            "POST",
            f"/tools/{quote(tool_name, safe='')}",
            payload=params,
        )
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Failed to call project tool: {exc}")]

async def handle_run_in_box(args):
    project_name = args["project"]
    command = args["command"]
    timeout_s = min(int(args.get("timeout", 30)), 120)

    log_query("run_in_box", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        container_name = box["container_name"]

        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )

        with db_connection() as conn:
            conn.execute(
                "UPDATE project_boxes SET last_healthcheck = CURRENT_TIMESTAMP, status = 'running', error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (project["id"],),
            )
            conn.commit()

        payload = {
            "project": project["name"],
            "command": command,
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
            "container": container_name,
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text=f"Command timed out after {timeout_s}s.")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Failed to run in box: {exc}")]

async def handle_install_deps(args):
    project_name = args["project"]
    packages = args.get("packages", [])
    manager = args.get("manager")

    log_query("install_deps", project_name, args)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    try:
        box = _ensure_project_box(project)
        box = _ensure_box_running(project, box)
        container_name = box["container_name"]
        project_path = _to_native_path(project["path"])

        if not manager:
            manager = "npm" if os.path.isfile(os.path.join(project_path, "package.json")) else "pip"

        if packages:
            import shlex

            safe_packages = " ".join(shlex.quote(pkg) for pkg in packages)
            install_cmd = f"pip install {safe_packages}" if manager == "pip" else f"npm install {safe_packages}"
        else:
            install_cmd = "pip install -r requirements.txt" if manager == "pip" else "npm install"

        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name, "bash", "-c", install_cmd],
            capture_output=True,
            text=True,
            timeout=180,
        )

        payload = {
            "project": project["name"],
            "manager": manager,
            "packages": packages if packages else "from manifest",
            "exit_code": result.returncode,
            "stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Install timed out after 180 seconds.")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Failed to install deps: {exc}")]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def register_tool(conn, **params):
    return _unwrap(await handle_register_tool(params))


async def get_tool_registry(conn, **params):
    return _unwrap(await handle_get_tool_registry(params))


async def get_tool(conn, **params):
    return _unwrap(await handle_get_tool(params))


async def create_tool(conn, **params):
    return _unwrap(await handle_create_tool(params))


async def update_tool(conn, **params):
    return _unwrap(await handle_update_tool(params))


async def box_status(conn, **params):
    return _unwrap(await handle_box_status(params))


async def box_logs(conn, **params):
    return _unwrap(await handle_box_logs(params))


async def list_project_tools(conn, **params):
    return _unwrap(await handle_list_project_tools(params))


async def call_project_tool(conn, **params):
    return _unwrap(await handle_call_project_tool(params))


async def run_in_box(conn, **params):
    return _unwrap(await handle_run_in_box(params))


async def install_deps(conn, **params):
    return _unwrap(await handle_install_deps(params))
