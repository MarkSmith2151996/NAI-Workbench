from __future__ import annotations

import asyncio
import json
import os
import platform as _platform
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from custodian.db.connection import db_connection


WORKSTATION_ROOT = Path(os.environ.get("CUSTODIAN_WORKSTATION_ROOT", "/home/dev/.workbench/workstations"))
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_DISPATCH_COUNTS: dict[str, int] = {}
_QUEUE_DEPTHS: dict[str, int] = {}


def _check_wsl() -> bool:
    if _platform.system() != "Linux" or not os.path.exists("/proc/version"):
        return False
    with open("/proc/version", encoding="utf-8") as handle:
        return "microsoft" in handle.read().lower()


_IS_WSL = _check_wsl()


def _to_native_path(path: str | None) -> str | None:
    if not _IS_WSL or not path:
        return path
    match = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def _ensure_schema() -> None:
    from custodian.db.migrations import run_all_migrations

    run_all_migrations()
    with db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workstation_specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                services TEXT NOT NULL DEFAULT '[]',
                deps TEXT NOT NULL DEFAULT '[]',
                env_vars TEXT NOT NULL DEFAULT '{}',
                volumes TEXT NOT NULL DEFAULT '[]',
                tool_definitions TEXT NOT NULL DEFAULT '[]',
                image TEXT NOT NULL DEFAULT 'nai-sandbox:latest',
                max_slots INTEGER NOT NULL DEFAULT 10,
                browser_profile TEXT,
                created_by TEXT NOT NULL DEFAULT 'claude',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS workstation_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spec_id INTEGER NOT NULL REFERENCES workstation_specs(id),
                container_name TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'provisioning',
                error_message TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS workstation_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL REFERENCES workstation_instances(id),
                slot_index INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'free',
                agent_run_id INTEGER,
                working_dir TEXT,
                output_dir TEXT,
                allocated_at TEXT,
                released_at TEXT,
                UNIQUE(instance_id, slot_index)
            );

            CREATE INDEX IF NOT EXISTS idx_workstation_specs_status ON workstation_specs(status);
            CREATE INDEX IF NOT EXISTS idx_workstation_instances_spec ON workstation_instances(spec_id);
            CREATE INDEX IF NOT EXISTS idx_workstation_slots_instance_status ON workstation_slots(instance_id, status);
            """
        )
        _ensure_column(conn, "workstation_specs", "tool_definitions", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "agents", "workstation", "TEXT")
        conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _validate_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("name is required")
    if not _NAME_RE.match(cleaned):
        raise ValueError("name must start with an alphanumeric character and contain only letters, numbers, dots, underscores, or hyphens")
    return cleaned


def _container_name(spec_name: str) -> str:
    return f"ws-{spec_name}"


def _validate_services(services: Any) -> list[dict[str, Any]]:
    if services is None:
        return []
    if not isinstance(services, list):
        raise ValueError("services must be a list")
    validated: list[dict[str, Any]] = []
    for index, service in enumerate(services):
        if not isinstance(service, dict):
            raise ValueError(f"services[{index}] must be an object")
        missing = [key for key in ("name", "host", "port") if key not in service]
        if missing:
            raise ValueError(f"services[{index}] missing required field(s): {', '.join(missing)}")
        try:
            port = int(service["port"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"services[{index}].port must be an integer") from exc
        if port < 1 or port > 65535:
            raise ValueError(f"services[{index}].port must be between 1 and 65535")
        normalized = dict(service)
        normalized["name"] = str(normalized["name"])
        normalized["host"] = str(normalized["host"])
        normalized["port"] = port
        validated.append(normalized)
    return validated


def _validate_list(value: Any, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


def _validate_env_vars(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("env_vars must be an object")
    return {str(key): str(val) for key, val in value.items()}


def _validate_tool_definitions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tool_definitions must be a list")
    validated: list[dict[str, Any]] = []
    for index, tool in enumerate(value):
        if not isinstance(tool, dict):
            raise ValueError(f"tool_definitions[{index}] must be an object")
        name = str(tool.get("name") or "").strip()
        if not name:
            raise ValueError(f"tool_definitions[{index}] missing required field: name")
        if not tool.get("command_template") and not tool.get("handler"):
            raise ValueError(f"tool_definitions[{index}] must define command_template or handler")
        normalized = dict(tool)
        normalized["name"] = name
        normalized["description"] = str(normalized.get("description") or "")
        normalized["input_schema"] = normalized.get("input_schema") or normalized.get("params") or {"type": "object", "properties": {}}
        validated.append(normalized)
    return validated


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _spec_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, fallback in (("services", []), ("deps", []), ("env_vars", {}), ("volumes", []), ("tool_definitions", [])):
        data[key] = _json_loads(data.get(key), fallback)
    return data


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _slot_dict(row: sqlite3.Row | None, instance: sqlite3.Row | dict[str, Any] | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if instance is not None:
        data["instance"] = dict(instance)
    return data


def _inspect_container(container_name: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}|{{.Config.Image}}|{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {"exists": False, "running": False, "error": str(exc)}
    if result.returncode != 0:
        return {"exists": False, "running": False, "error": (result.stderr or result.stdout).strip()}
    container_id, image, running = (result.stdout.strip().split("|", 2) + ["", "", ""])[:3]
    return {
        "exists": True,
        "container_id": container_id[:12],
        "image": image,
        "running": running.strip().lower() == "true",
        "error": None,
    }


def _run_docker(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"command failed: {' '.join(command)}").strip())
    return result


def _workstation_path(spec_name: str) -> Path:
    return WORKSTATION_ROOT / spec_name


def _normalize_volume(volume: Any) -> tuple[str, str, bool]:
    if isinstance(volume, str):
        parts = volume.split(":")
        if len(parts) < 2:
            raise ValueError("volume strings must be 'host_path:container_path[:ro]'")
        host_path = parts[0]
        container_path = parts[1]
        read_only = len(parts) > 2 and parts[2].lower() == "ro"
    elif isinstance(volume, dict):
        host_path = str(volume.get("host") or volume.get("source") or "")
        container_path = str(volume.get("container") or volume.get("target") or "")
        read_only = bool(volume.get("read_only") or volume.get("readonly"))
    else:
        raise ValueError("volumes must contain strings or objects")
    host_path = str(_to_native_path(host_path) or "")
    if not host_path or not container_path:
        raise ValueError("volume host/source and container/target paths are required")
    if not container_path.startswith("/"):
        raise ValueError("volume container path must be absolute")
    return host_path, container_path, read_only


def _volume_args(volumes: list[Any]) -> list[str]:
    args: list[str] = []
    for volume in volumes:
        host_path, container_path, read_only = _normalize_volume(volume)
        suffix = ":ro" if read_only else ""
        args.extend(["-v", f"{host_path}:{container_path}{suffix}"])
    return args


def _create_slot_dirs(container_name: str, max_slots: int) -> None:
    commands = [f"mkdir -p /workspace/slots/{index}/output" for index in range(max_slots)]
    _run_docker(["docker", "exec", container_name, "bash", "-lc", " && ".join(commands)], timeout=60)


def _install_deps(container_name: str, deps: list[Any]) -> list[str]:
    warnings: list[str] = []
    pip_deps: list[str] = []
    npm_deps: list[str] = []
    for dep in deps:
        if isinstance(dep, dict):
            manager = str(dep.get("manager") or dep.get("type") or "pip").lower()
            package = str(dep.get("package") or dep.get("name") or "").strip()
        else:
            manager = "pip"
            package = str(dep).strip()
        if not package:
            continue
        if manager == "npm":
            npm_deps.append(package)
        elif manager == "pip":
            pip_deps.append(package)
        else:
            warnings.append(f"unsupported dependency manager {manager!r} for {package!r}")

    if pip_deps:
        safe = " ".join(shlex.quote(dep) for dep in pip_deps)
        _run_docker(["docker", "exec", "-w", "/workspace", container_name, "bash", "-lc", f"pip install {safe}"], timeout=300)
    if npm_deps:
        safe = " ".join(shlex.quote(dep) for dep in npm_deps)
        _run_docker(["docker", "exec", "-w", "/workspace", container_name, "bash", "-lc", f"npm install {safe}"], timeout=300)
    return warnings


def _health_check_services(services: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for service in services:
        name = service["name"]
        host = service["host"]
        port = int(service["port"])
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError as exc:
            warning = f"service {name} at {host}:{port} unreachable: {exc}"
            print(f"[workstation] {warning}", file=sys.stderr)
            warnings.append(warning)
    return warnings


def _upsert_instance(conn: sqlite3.Connection, spec_id: int, container_name: str, status: str, error_message: str | None = None) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO workstation_instances (spec_id, container_name, status, error_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(container_name) DO UPDATE SET
            spec_id = excluded.spec_id,
            status = excluded.status,
            error_message = excluded.error_message,
            updated_at = CURRENT_TIMESTAMP
        """,
        (spec_id, container_name, status, error_message),
    )
    return conn.execute("SELECT * FROM workstation_instances WHERE container_name = ?", (container_name,)).fetchone()


def _ensure_slot_rows(conn: sqlite3.Connection, instance_id: int, max_slots: int) -> None:
    for index in range(max_slots):
        working_dir = f"/workspace/slots/{index}"
        output_dir = f"/workspace/slots/{index}/output"
        conn.execute(
            """
            INSERT OR IGNORE INTO workstation_slots (instance_id, slot_index, status, working_dir, output_dir)
            VALUES (?, ?, 'free', ?, ?)
            """,
            (instance_id, index, working_dir, output_dir),
        )


def create_spec(
    name: str,
    description: str | None = None,
    services: list[dict[str, Any]] | None = None,
    deps: list[Any] | None = None,
    env_vars: dict[str, Any] | None = None,
    volumes: list[Any] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    image: str | None = None,
    max_slots: int = 10,
    browser_profile: str | None = None,
    created_by: str = "claude",
) -> dict[str, Any]:
    _ensure_schema()
    spec_name = _validate_name(name)
    valid_services = _validate_services(services or [])
    valid_deps = _validate_list(deps, "deps")
    valid_env = _validate_env_vars(env_vars)
    valid_volumes = _validate_list(volumes, "volumes")
    valid_tools = _validate_tool_definitions(tool_definitions)
    slot_count = int(max_slots or 10)
    if slot_count < 1:
        raise ValueError("max_slots must be >= 1")
    with db_connection() as conn:
        existing = conn.execute("SELECT * FROM workstation_specs WHERE name = ?", (spec_name,)).fetchone()
        if existing and existing["status"] != "retired":
            raise ValueError(f"workstation spec already exists: {spec_name}")
        if existing:
            conn.execute(
                """
                UPDATE workstation_specs
                SET description = ?, services = ?, deps = ?, env_vars = ?, volumes = ?, tool_definitions = ?, image = ?,
                    max_slots = ?, browser_profile = ?, created_by = ?, status = 'active', updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
                """,
                (
                    description,
                    _json_dumps(valid_services),
                    _json_dumps(valid_deps),
                    _json_dumps(valid_env),
                    _json_dumps(valid_volumes),
                    _json_dumps(valid_tools),
                    image or "nai-sandbox:latest",
                    slot_count,
                    browser_profile,
                    created_by or "claude",
                    spec_name,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workstation_specs WHERE name = ?", (spec_name,)).fetchone()
            return _spec_dict(row) or {}
        conn.execute(
            """
            INSERT INTO workstation_specs (
                name, description, services, deps, env_vars, volumes, tool_definitions, image,
                max_slots, browser_profile, created_by, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                spec_name,
                description,
                _json_dumps(valid_services),
                _json_dumps(valid_deps),
                _json_dumps(valid_env),
                _json_dumps(valid_volumes),
                _json_dumps(valid_tools),
                image or "nai-sandbox:latest",
                slot_count,
                browser_profile,
                created_by or "claude",
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM workstation_specs WHERE name = ?", (spec_name,)).fetchone()
    return _spec_dict(row) or {}


def update_spec(name: str, **kwargs: Any) -> dict[str, Any]:
    _ensure_schema()
    spec_name = _validate_name(name)
    if "name" in kwargs:
        raise ValueError("changing workstation spec name is not allowed")
    allowed = {"description", "services", "deps", "env_vars", "volumes", "tool_definitions", "image", "max_slots", "browser_profile", "status"}
    updates: list[str] = []
    values: list[Any] = []
    for key, value in kwargs.items():
        if key not in allowed or value is None:
            continue
        if key == "services":
            value = _json_dumps(_validate_services(value))
        elif key in {"deps", "volumes"}:
            value = _json_dumps(_validate_list(value, key))
        elif key == "tool_definitions":
            value = _json_dumps(_validate_tool_definitions(value))
        elif key == "env_vars":
            value = _json_dumps(_validate_env_vars(value))
        elif key == "max_slots":
            value = int(value)
            if value < 1:
                raise ValueError("max_slots must be >= 1")
        updates.append(f"{key} = ?")
        values.append(value)
    if not updates:
        spec = get_spec(spec_name)
        if spec is None:
            raise ValueError(f"workstation spec not found: {spec_name}")
        return spec
    values.append(spec_name)
    with db_connection() as conn:
        cursor = conn.execute(
            f"UPDATE workstation_specs SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            values,
        )
        if cursor.rowcount == 0:
            raise ValueError(f"workstation spec not found: {spec_name}")
        conn.commit()
        row = conn.execute("SELECT * FROM workstation_specs WHERE name = ?", (spec_name,)).fetchone()
    return _spec_dict(row) or {}


def get_spec(name: str) -> dict[str, Any] | None:
    _ensure_schema()
    spec_name = _validate_name(name)
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM workstation_specs WHERE name = ?", (spec_name,)).fetchone()
    return _spec_dict(row)


def list_specs(status: str = "active") -> list[dict[str, Any]]:
    _ensure_schema()
    with db_connection() as conn:
        if status:
            rows = conn.execute("SELECT * FROM workstation_specs WHERE status = ? ORDER BY name", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM workstation_specs ORDER BY name").fetchall()
    return [_spec_dict(row) or {} for row in rows]


def provision_instance(spec_name: str) -> dict[str, Any]:
    _ensure_schema()
    spec = get_spec(spec_name)
    if spec is None:
        raise ValueError(f"workstation spec not found: {spec_name}")
    if spec["status"] != "active":
        raise ValueError(f"workstation spec is not active: {spec_name}")

    container_name = _container_name(spec["name"])
    inspect = _inspect_container(container_name)
    with db_connection() as conn:
        instance = conn.execute(
            "SELECT * FROM workstation_instances WHERE spec_id = ? AND container_name = ?",
            (spec["id"], container_name),
        ).fetchone()
        if instance and instance["status"] == "warm" and inspect["exists"] and inspect["running"]:
            _ensure_slot_rows(conn, int(instance["id"]), int(spec["max_slots"]))
            conn.commit()
            return {"instance": _row_dict(instance), "health_warnings": []}

    data_dir = _workstation_path(spec["name"])
    data_dir.mkdir(parents=True, exist_ok=True)
    image = spec["image"] or "nai-sandbox:latest"
    env_vars = spec["env_vars"]
    volumes = spec["volumes"]
    health_warnings: list[str] = []

    try:
        if inspect["exists"] and not inspect["running"]:
            _run_docker(["docker", "start", container_name], timeout=30)
        elif not inspect["exists"]:
            run_cmd = [
                "docker",
                "run",
                "-d",
                "--network",
                "host",
                "--name",
                container_name,
                "-v",
                f"{data_dir}:/workspace",
                "-w",
                "/workspace",
            ]
            for key, value in sorted(env_vars.items()):
                run_cmd.extend(["-e", f"{key}={value}"])
            run_cmd.extend(_volume_args(volumes))
            run_cmd.extend([image, "sleep", "infinity"])
            _run_docker(run_cmd, timeout=90)

        _create_slot_dirs(container_name, int(spec["max_slots"]))
        health_warnings.extend(_install_deps(container_name, spec["deps"]))
        health_warnings.extend(_health_check_services(spec["services"]))

        with db_connection() as conn:
            instance = _upsert_instance(conn, int(spec["id"]), container_name, "warm", None)
            _ensure_slot_rows(conn, int(instance["id"]), int(spec["max_slots"]))
            conn.commit()
            instance = conn.execute("SELECT * FROM workstation_instances WHERE id = ?", (instance["id"],)).fetchone()
        return {"instance": _row_dict(instance), "health_warnings": health_warnings}
    except Exception as exc:
        with db_connection() as conn:
            instance = _upsert_instance(conn, int(spec["id"]), container_name, "error", str(exc))
            conn.commit()
        return {"instance": _row_dict(instance), "health_warnings": health_warnings, "error": str(exc)}


def allocate_slot(spec_name: str, agent_run_id: int | None = None) -> dict[str, Any]:
    provisioned = provision_instance(spec_name)
    if provisioned.get("error"):
        raise RuntimeError(str(provisioned["error"]))
    instance = provisioned["instance"]
    container_name = instance["container_name"]
    with db_connection() as conn:
        slot = conn.execute(
            """
            SELECT * FROM workstation_slots
            WHERE instance_id = ? AND status IN ('free', 'released')
            ORDER BY slot_index
            LIMIT 1
            """,
            (instance["id"],),
        ).fetchone()
        if slot is None:
            raise RuntimeError(f"no free slots available for workstation {spec_name}")
        conn.execute(
            """
            UPDATE workstation_slots
            SET status = 'allocated', agent_run_id = ?, allocated_at = CURRENT_TIMESTAMP, released_at = NULL
            WHERE id = ?
            """,
            (agent_run_id, slot["id"]),
        )
        conn.commit()
        slot = conn.execute("SELECT * FROM workstation_slots WHERE id = ?", (slot["id"],)).fetchone()

    _run_docker(
        ["docker", "exec", container_name, "bash", "-lc", f"mkdir -p {shlex.quote(slot['working_dir'])} {shlex.quote(slot['output_dir'])}"],
        timeout=30,
    )
    return _slot_dict(slot, instance) or {}


def allocate_slots(spec_name: str, count: int, agent_run_id: int | None = None) -> list[dict[str, Any]]:
    requested = int(count or 0)
    if requested < 1:
        return []
    provisioned = provision_instance(spec_name)
    if provisioned.get("error"):
        raise RuntimeError(str(provisioned["error"]))
    spec = get_spec(spec_name)
    if spec is None:
        raise ValueError(f"workstation spec not found: {spec_name}")
    max_slots = int(spec["max_slots"] or 1)
    if requested > max_slots:
        raise RuntimeError(f"requested {requested} slots for workstation {spec_name}, but max_slots is {max_slots}")
    instance = provisioned["instance"]
    container_name = instance["container_name"]
    with db_connection() as conn:
        slots = conn.execute(
            """
            SELECT * FROM workstation_slots
            WHERE instance_id = ? AND status IN ('free', 'released')
            ORDER BY slot_index
            LIMIT ?
            """,
            (instance["id"], requested),
        ).fetchall()
        if len(slots) < requested:
            raise RuntimeError(f"only {len(slots)} free slot(s) available for workstation {spec_name}; requested {requested}")
        slot_ids = [slot["id"] for slot in slots]
        placeholders = ", ".join("?" for _ in slot_ids)
        conn.execute(
            f"""
            UPDATE workstation_slots
            SET status = 'allocated', agent_run_id = ?, allocated_at = CURRENT_TIMESTAMP, released_at = NULL
            WHERE id IN ({placeholders})
            """,
            [agent_run_id, *slot_ids],
        )
        conn.commit()
        allocated = conn.execute(
            f"SELECT * FROM workstation_slots WHERE id IN ({placeholders}) ORDER BY slot_index",
            slot_ids,
        ).fetchall()

    mkdirs = " && ".join(
        f"mkdir -p {shlex.quote(slot['working_dir'])} {shlex.quote(slot['output_dir'])}" for slot in allocated
    )
    _run_docker(["docker", "exec", container_name, "bash", "-lc", mkdirs], timeout=30)
    return [_slot_dict(slot, instance) or {} for slot in allocated]


def release_slot(slot_id: int) -> dict[str, Any]:
    _ensure_schema()
    with db_connection() as conn:
        slot = conn.execute("SELECT * FROM workstation_slots WHERE id = ?", (int(slot_id),)).fetchone()
        if slot is None:
            raise ValueError(f"workstation slot not found: {slot_id}")
        conn.execute(
            "UPDATE workstation_slots SET status = 'released', released_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(slot_id),),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM workstation_slots WHERE id = ?", (int(slot_id),)).fetchone()
    return _slot_dict(updated) or {}


def release_slots(slot_ids: list[int]) -> list[dict[str, Any]]:
    ids = [int(slot_id) for slot_id in slot_ids]
    if not ids:
        return []
    _ensure_schema()
    placeholders = ", ".join("?" for _ in ids)
    with db_connection() as conn:
        conn.execute(
            f"UPDATE workstation_slots SET status = 'released', released_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        rows = conn.execute(f"SELECT * FROM workstation_slots WHERE id IN ({placeholders}) ORDER BY slot_index", ids).fetchall()
    return [_slot_dict(row) or {} for row in rows]


def available_slot_count(spec_name: str) -> int:
    status = get_instance_status(spec_name)
    slots = status.get("slots") or {}
    return int(slots.get("free") or 0) + int(slots.get("released") or 0)


def get_agent_workstation(agent_name: str) -> str | None:
    _ensure_schema()
    with db_connection() as conn:
        agent = conn.execute("SELECT * FROM agents WHERE name = ? AND status = 'active'", (agent_name,)).fetchone()
        if not agent:
            return None
        spec_data = _load_yaml_spec_for_agent(conn, agent)
        workstation = agent["workstation"] or (spec_data or {}).get("workstation")
        return str(workstation) if workstation else None


def note_queue_depth(spec_name: str, depth: int) -> None:
    _QUEUE_DEPTHS[spec_name] = max(0, int(depth or 0))


def get_instance_status(spec_name: str) -> dict[str, Any]:
    _ensure_schema()
    spec = get_spec(spec_name)
    if spec is None:
        raise ValueError(f"workstation spec not found: {spec_name}")
    with db_connection() as conn:
        instance = conn.execute(
            "SELECT * FROM workstation_instances WHERE spec_id = ? ORDER BY id DESC LIMIT 1",
            (spec["id"],),
        ).fetchone()
        counts = {"free": 0, "allocated": 0, "released": 0}
        if instance is not None:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM workstation_slots WHERE instance_id = ? GROUP BY status",
                (instance["id"],),
            ).fetchall()
            for row in rows:
                counts[row["status"]] = int(row["count"])
    container = _inspect_container(instance["container_name"]) if instance else None
    active_dispatches = int(counts.get("allocated") or 0)
    return {
        "spec": spec,
        "instance": _row_dict(instance),
        "slots": counts,
        "container": container,
        "active_dispatches": active_dispatches,
        "queue_depth": _QUEUE_DEPTHS.get(spec_name, 0),
        "completed_dispatches": _DISPATCH_COUNTS.get(spec_name, 0),
    }


def list_statuses() -> list[dict[str, Any]]:
    return [get_instance_status(spec["name"]) for spec in list_specs(status="active")]


def retire_workstation(spec_name: str) -> dict[str, Any]:
    _ensure_schema()
    spec = get_spec(spec_name)
    if spec is None:
        raise ValueError(f"workstation spec not found: {spec_name}")
    container_name = _container_name(spec["name"])
    inspect = _inspect_container(container_name)
    if inspect["exists"]:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True, timeout=60)
    with db_connection() as conn:
        instance = conn.execute(
            "SELECT * FROM workstation_instances WHERE spec_id = ? AND container_name = ?",
            (spec["id"], container_name),
        ).fetchone()
        if instance is not None:
            conn.execute(
                "UPDATE workstation_slots SET status = 'released', released_at = COALESCE(released_at, CURRENT_TIMESTAMP) WHERE instance_id = ? AND status = 'allocated'",
                (instance["id"],),
            )
            conn.execute(
                "UPDATE workstation_instances SET status = 'stopped', error_message = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (instance["id"],),
            )
        conn.execute("UPDATE workstation_specs SET status = 'retired', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (spec["id"],))
        conn.commit()
        instance = conn.execute(
            "SELECT * FROM workstation_instances WHERE spec_id = ? AND container_name = ?",
            (spec["id"], container_name),
        ).fetchone()
        spec_row = conn.execute("SELECT * FROM workstation_specs WHERE id = ?", (spec["id"],)).fetchone()
    return {"spec": _spec_dict(spec_row), "instance": _row_dict(instance), "container_removed": True}


def exec_in_workstation(spec_name: str, command: str, slot_index: int | None = None, timeout: int = 30) -> dict[str, Any]:
    status = get_instance_status(spec_name)
    instance = status.get("instance")
    if not instance or instance.get("status") != "warm" or not status.get("container", {}).get("running"):
        provisioned = provision_instance(spec_name)
        if provisioned.get("error"):
            raise RuntimeError(str(provisioned["error"]))
        instance = provisioned["instance"]
    workdir = "/workspace"
    if slot_index is not None:
        workdir = f"/workspace/slots/{int(slot_index)}"
    result = subprocess.run(
        ["docker", "exec", "-w", workdir, instance["container_name"], "bash", "-lc", command],
        capture_output=True,
        text=True,
        timeout=min(int(timeout or 30), 300),
    )
    return {
        "spec": spec_name,
        "container": instance["container_name"],
        "workdir": workdir,
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:] if len(result.stdout) > 4000 else result.stdout,
        "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
    }


def _copy_agent_loop(container_name: str) -> None:
    source = Path(__file__).with_name("agent_loop.py")
    _run_docker(["docker", "cp", str(source), f"{container_name}:/workspace/agent_loop.py"], timeout=30)


def _write_container_file(container_name: str, path: str, content: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_name,
            "python3",
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')",
            path,
        ],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to write container file").strip())


def _read_container_file(container_name: str, path: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container_name, "python3", "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(encoding='utf-8'))", path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to read container file").strip())
    return result.stdout


def _run_slot_payload(
    *,
    spec_name: str,
    slot: dict[str, Any],
    task: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    model: str,
) -> dict[str, Any]:
    instance = slot["instance"]
    container_name = instance["container_name"]
    _copy_agent_loop(container_name)
    task_payload = {
        "task": task,
        "system_prompt": system_prompt,
        "tools": tools,
        "model": model,
        "working_dir": slot["working_dir"],
        "output_dir": slot["output_dir"],
    }
    task_path = f"{slot['working_dir']}/task.json"
    result_path = f"{slot['output_dir']}/result.json"
    _write_container_file(container_name, task_path, json.dumps(task_payload, indent=2))
    result = subprocess.run(
        ["docker", "exec", "-w", slot["working_dir"], container_name, "python3", "/workspace/agent_loop.py", "--task-file", "task.json"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "agent loop failed").strip())
    result_payload = json.loads(_read_container_file(container_name, result_path))
    result_payload["workstation"] = spec_name
    result_payload["slot_id"] = slot["id"]
    result_payload["slot_index"] = slot["slot_index"]
    _DISPATCH_COUNTS[spec_name] = _DISPATCH_COUNTS.get(spec_name, 0) + 1
    return result_payload


def _merge_tools(base_tools: list[dict[str, Any]], override_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for tool in [*base_tools, *(override_tools or [])]:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        merged[str(tool["name"])] = tool
    return list(merged.values())


def _load_agent_specific_tools(agent: sqlite3.Row | dict[str, Any], spec_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    raw_tools = agent.get("tools") if isinstance(agent, dict) else agent["tools"]
    if raw_tools:
        decoded = _json_loads(raw_tools, [])
        if isinstance(decoded, list):
            tools.extend(item for item in decoded if isinstance(item, dict))
    if spec_data:
        spec_tools = spec_data.get("tools") or []
        if isinstance(spec_tools, list):
            tools.extend(item for item in spec_tools if isinstance(item, dict))
    return _validate_tool_definitions(tools) if tools else []


def _load_yaml_spec_for_agent(conn: sqlite3.Connection, agent: sqlite3.Row) -> dict[str, Any] | None:
    spec_path = agent["spec_path"]
    project_id = agent["project_id"]
    if not spec_path or not project_id:
        return None
    project = conn.execute("SELECT path FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return None
    path = Path(_to_native_path(project["path"]) or "") / str(spec_path).replace("\\", "/")
    if not path.exists():
        return None
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _agent_runtime(agent_name: str) -> dict[str, Any]:
    _ensure_schema()
    with db_connection() as conn:
        agent = conn.execute("SELECT * FROM agents WHERE name = ? AND status = 'active'", (agent_name,)).fetchone()
        if not agent:
            raise ValueError(f"agent not found or inactive: {agent_name}")
        spec_data = _load_yaml_spec_for_agent(conn, agent)
        workstation = agent["workstation"] or (spec_data or {}).get("workstation")
        if not workstation:
            raise ValueError(f"agent '{agent_name}' is not bound to a workstation")
        ws_spec = conn.execute("SELECT status FROM workstation_specs WHERE name = ?", (workstation,)).fetchone()
        if not ws_spec or ws_spec["status"] != "active":
            raise ValueError(f"workstation spec not found or inactive: {workstation}")
        return {
            "workstation": str(workstation),
            "tools": _load_agent_specific_tools(agent, spec_data),
            "system_prompt": str(agent["system_prompt"] or (spec_data or {}).get("task") or ""),
            "model": str(agent["model"] or (spec_data or {}).get("model") or "gpt-5.4"),
        }


def dispatch_to_slot(
    spec_name: str,
    task: str,
    system_prompt: str,
    tools_override: list[dict[str, Any]] | None = None,
    model: str | None = None,
    agent_run_id: int | None = None,
) -> dict[str, Any]:
    spec = get_spec(spec_name)
    if spec is None:
        raise ValueError(f"workstation spec not found: {spec_name}")
    tools = _merge_tools(spec.get("tool_definitions") or [], tools_override)
    slot = allocate_slot(spec_name, agent_run_id=agent_run_id)
    try:
        return _run_slot_payload(
            spec_name=spec_name,
            slot=slot,
            task=task,
            system_prompt=system_prompt,
            tools=tools,
            model=model or "gpt-5.4",
        )
    finally:
        release_slot(int(slot["id"]))


def dispatch_agent(agent_name: str, task: str, agent_run_id: int | None = None) -> dict[str, Any]:
    runtime = _agent_runtime(agent_name)
    return dispatch_to_slot(
        spec_name=runtime["workstation"],
        task=task,
        system_prompt=runtime["system_prompt"],
        tools_override=runtime["tools"],
        model=runtime["model"],
        agent_run_id=agent_run_id,
    )


def dispatch_batch(agent_name: str, tasks: list[str], parallel: int | None = None) -> dict[str, Any]:
    task_list = [str(task) for task in tasks]
    if not task_list:
        return {"completed": 0, "failed": 0, "results": []}
    runtime = _agent_runtime(agent_name)
    spec = get_spec(runtime["workstation"])
    if spec is None:
        raise ValueError(f"workstation spec not found: {runtime['workstation']}")
    max_slots = int(spec["max_slots"] or 1)
    requested_parallel = int(parallel or len(task_list) or 1)
    if requested_parallel < 1:
        requested_parallel = 1
    batch_size = min(requested_parallel, max_slots)
    if requested_parallel > max_slots:
        print(
            f"[workstation] capping parallel dispatch for {runtime['workstation']} from {requested_parallel} to max_slots {max_slots}",
            file=sys.stderr,
        )

    results: list[dict[str, Any] | None] = [None] * len(task_list)

    async def run_slot(task_index: int, slot: dict[str, Any]) -> None:
        try:
            payload = await asyncio.to_thread(
                _run_slot_payload,
                spec_name=runtime["workstation"],
                slot=slot,
                task=task_list[task_index],
                system_prompt=runtime["system_prompt"],
                tools=_merge_tools(spec.get("tool_definitions") or [], runtime["tools"]),
                model=runtime["model"],
            )
            failed_tool = next(
                (
                    call
                    for call in payload.get("tool_calls_made", [])
                    if isinstance(call, dict) and isinstance(call.get("result"), dict) and call["result"].get("ok") is False
                ),
                None,
            )
            payload["ok"] = failed_tool is None
            if failed_tool is not None:
                payload["error"] = failed_tool["result"].get("error") or failed_tool["result"].get("stderr") or "tool execution failed"
            payload["task_index"] = task_index
            results[task_index] = payload
        except Exception as exc:  # noqa: BLE001 - per-task failure should not abort batch
            results[task_index] = {"ok": False, "error": str(exc), "task": task_list[task_index], "task_index": task_index}

    async def run_all() -> None:
        for start in range(0, len(task_list), batch_size):
            end = min(start + batch_size, len(task_list))
            slots = allocate_slots(runtime["workstation"], end - start)
            try:
                await asyncio.gather(*(run_slot(index, slot) for index, slot in zip(range(start, end), slots)))
            finally:
                release_slots([int(slot["id"]) for slot in slots])

    asyncio.run(run_all())
    final_results = [result or {"ok": False, "error": "task did not run", "task": task_list[index], "task_index": index} for index, result in enumerate(results)]
    completed = len([result for result in final_results if result.get("ok")])
    failed = len(final_results) - completed
    return {"completed": completed, "failed": failed, "results": final_results}
