#!/usr/bin/env python3
"""REST bridge for cross-box Custodian operations."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error, request

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


DB_PATH = Path("/home/dev/projects/nai-workbench/custodian/custodian.db")
BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = 9099


app = FastAPI(title="Custodian Box Bridge")


class RunRequest(BaseModel):
    project: str
    command: str
    timeout: int = Field(default=30, ge=1, le=600)


class CallToolRequest(BaseModel):
    project: str
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)


class RegisterToolRequest(BaseModel):
    name: str
    project: str
    description: str = ""
    params_schema: dict[str, Any] = Field(default_factory=dict)
    handler_path: str
    source_module: str | None = None
    source_class: str | None = None
    source_method: str | None = None
    hook_point: str | None = None
    return_type: str | None = None
    known_side_effects: str | None = None
    created_by: str = "box-bridge"


def _db_connection(read_only: bool = True) -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail=f"database not found: {DB_PATH}")
    if read_only:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_project(project_name: str) -> sqlite3.Row:
    try:
        with _db_connection() as conn:
            row = conn.execute(
                "SELECT id, name, path, stack, status FROM projects WHERE name = ?",
                (project_name,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_name}")
    return row


def _get_project_box(project_id: int) -> sqlite3.Row | None:
    try:
        with _db_connection() as conn:
            return conn.execute(
                """
                SELECT container_name, status, tool_server_port, last_healthcheck
                FROM project_boxes
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc


def _container_name_for(project: sqlite3.Row, box_row: sqlite3.Row | None) -> str:
    if box_row and box_row["container_name"]:
        return str(box_row["container_name"])
    return f"alpha-{project['name']}"


def _docker_running(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=502, detail=f"docker inspect timed out for {container_name}") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"docker inspect failed: {exc}") from exc
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _ensure_running(container_name: str) -> None:
    if not _docker_running(container_name):
        raise HTTPException(status_code=502, detail=f"container not running: {container_name}")


def _container_file_path(value: str, field_name: str = "path") -> PurePosixPath:
    if not value:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if "\x00" in value:
        raise HTTPException(status_code=400, detail=f"{field_name} contains a null byte")
    if not value.startswith("/"):
        raise HTTPException(status_code=400, detail=f"{field_name} must be an absolute container path")
    return PurePosixPath(value)


def _docker_exec(container_name: str, command: str, timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "exec", "-w", "/workspace", container_name, "bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=502, detail=f"docker exec timed out after {timeout}s") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"docker exec failed: {exc}") from exc
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _docker_cp_to_container(container_name: str, source_path: Path, dest_path: PurePosixPath) -> None:
    try:
        result = subprocess.run(
            ["docker", "cp", str(source_path), f"{container_name}:{dest_path}"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=502, detail="docker cp upload timed out") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"docker cp upload failed: {exc}") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"docker cp upload failed: {result.stderr.strip()}")


def _docker_cp_from_container(container_name: str, source_path: PurePosixPath, dest_path: Path) -> None:
    try:
        result = subprocess.run(
            ["docker", "cp", f"{container_name}:{source_path}", str(dest_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=502, detail="docker cp download timed out") from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"docker cp download failed: {exc}") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=404, detail=f"docker cp download failed: {result.stderr.strip()}")


def _container_file_size(container_name: str, file_path: PurePosixPath) -> int:
    result = _docker_exec(container_name, f"stat -c %s {shlex.quote(str(file_path))}", 30)
    if result["exit_code"] != 0:
        raise HTTPException(status_code=502, detail=f"file verification failed: {result['stderr'].strip()}")
    try:
        return int(result["stdout"].strip())
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="file verification returned an invalid size") from exc


def _error_response(status_code: int, message: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": str(message), "success": False})


def _parse_upload_form(content_type: str, body: bytes) -> tuple[str, str, bytes]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="Content-Type must be multipart/form-data")

    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=email_policy).parsebytes(header + body)
    if not message.is_multipart():
        raise HTTPException(status_code=400, detail="invalid multipart/form-data body")

    fields: dict[str, str] = {}
    file_bytes: bytes | None = None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        params = dict(part.get_params(header="content-disposition", unquote=True)[1:])
        name = params.get("name")
        payload = part.get_payload(decode=True) or b""
        if name in {"project", "dest_path"}:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset)
        elif name == "file":
            file_bytes = payload

    project = fields.get("project")
    dest_path = fields.get("dest_path")
    if not project:
        raise HTTPException(status_code=400, detail="project is required")
    if not dest_path:
        raise HTTPException(status_code=400, detail="dest_path is required")
    if file_bytes is None:
        raise HTTPException(status_code=400, detail="file is required")
    return project, dest_path, file_bytes


def _discover_tool_port(project: sqlite3.Row, box_row: sqlite3.Row | None) -> int:
    if box_row and box_row["tool_server_port"]:
        return int(box_row["tool_server_port"])
    raise HTTPException(status_code=404, detail=f"no tool server running for project {project['name']}")


def _forward_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"error": exc.reason}
        raise HTTPException(status_code=exc.code, detail=payload) from exc
    except error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"bridge request failed: {exc.reason}") from exc


def _tool_registry_exists() -> bool:
    try:
        with _db_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_registry'"
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc
    return row is not None


def _tool_registry_payload(body: RegisterToolRequest) -> tuple[Any, ...]:
    return (
        body.name,
        body.project,
        body.source_module or body.handler_path,
        body.source_class,
        body.source_method or "handler",
        body.hook_point or body.description or "registered via box bridge",
        body.return_type or "json",
        body.known_side_effects or "",
        body.handler_path,
        body.created_by,
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "port": BRIDGE_PORT}


@app.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    try:
        with _db_connection() as conn:
            rows = conn.execute(
                "SELECT name, path, stack, status FROM projects WHERE status = 'active' ORDER BY name"
            ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc
    return [dict(row) for row in rows]


@app.post("/run")
def run_in_box(body: RunRequest) -> dict[str, Any]:
    project = _get_project(body.project)
    box_row = _get_project_box(project["id"])
    container_name = _container_name_for(project, box_row)
    _ensure_running(container_name)
    return _docker_exec(container_name, body.command, body.timeout)


@app.post("/upload", response_model=None)
async def upload_file(request: Request) -> dict[str, Any] | JSONResponse:
    temp_path: Path | None = None
    try:
        project, dest_path, file_bytes = _parse_upload_form(
            request.headers.get("content-type", ""),
            await request.body(),
        )
        project_row = _get_project(project)
        box_row = _get_project_box(project_row["id"])
        container_name = _container_name_for(project_row, box_row)
        _ensure_running(container_name)

        container_path = _container_file_path(dest_path, "dest_path")
        parent = str(container_path.parent)
        mkdir_result = _docker_exec(container_name, f"mkdir -p {shlex.quote(parent)}", 30)
        if mkdir_result["exit_code"] != 0:
            raise HTTPException(
                status_code=502,
                detail=f"failed to create parent directory: {mkdir_result['stderr'].strip()}",
            )

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(file_bytes)

        _docker_cp_to_container(container_name, temp_path, container_path)
        size_bytes = _container_file_size(container_name, container_path)
        return {"project": project, "dest_path": str(container_path), "size_bytes": size_bytes, "success": True}
    except HTTPException as exc:
        return _error_response(exc.status_code, exc.detail)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


@app.get("/download", response_model=None)
def download_file(
    project: str = Query(...),
    file_path: str = Query(..., alias="path"),
) -> FileResponse | JSONResponse:
    temp_path: Path | None = None
    try:
        project_row = _get_project(project)
        box_row = _get_project_box(project_row["id"])
        container_name = _container_name_for(project_row, box_row)
        _ensure_running(container_name)

        container_path = _container_file_path(file_path)
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()

        _docker_cp_from_container(container_name, container_path, temp_path)
        filename = container_path.name or "download"
        return FileResponse(
            temp_path,
            media_type="application/octet-stream",
            filename=filename,
            background=BackgroundTask(lambda: os.unlink(temp_path)),
        )
    except HTTPException as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return _error_response(exc.status_code, exc.detail)


@app.post("/call-tool")
def call_tool(body: CallToolRequest) -> Any:
    project = _get_project(body.project)
    box_row = _get_project_box(project["id"])
    container_name = _container_name_for(project, box_row)
    _ensure_running(container_name)
    port = _discover_tool_port(project, box_row)
    return _forward_json("POST", f"http://localhost:{port}/tools/{body.tool_name}", body.params)


@app.get("/tools/{project_name}")
def list_tools(project_name: str) -> Any:
    project = _get_project(project_name)
    box_row = _get_project_box(project["id"])
    container_name = _container_name_for(project, box_row)
    _ensure_running(container_name)
    port = _discover_tool_port(project, box_row)
    return _forward_json("GET", f"http://localhost:{port}/tools")


@app.get("/status")
def status(project: str | None = Query(default=None)) -> dict[str, Any]:
    query = "SELECT id, name, path, stack, status FROM projects WHERE status = 'active'"
    params: tuple[Any, ...] = ()
    if project:
        query += " AND name = ?"
        params = (project,)

    try:
        with _db_connection() as conn:
            rows = conn.execute(query + " ORDER BY name", params).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc

    result = []
    for row in rows:
        box_row = _get_project_box(row["id"])
        container_name = _container_name_for(row, box_row)
        running = _docker_running(container_name)
        result.append(
            {
                "project": row["name"],
                "container": container_name,
                "running": running,
                "tool_server_port": int(box_row["tool_server_port"]) if box_row and box_row["tool_server_port"] else None,
                "box_status": box_row["status"] if box_row else None,
                "last_healthcheck": box_row["last_healthcheck"] if box_row else None,
            }
        )
    return {"projects": result}


@app.post("/register-tool")
def register_tool(body: RegisterToolRequest) -> dict[str, Any]:
    _get_project(body.project)
    if not _tool_registry_exists():
        raise HTTPException(status_code=500, detail="tool_registry table missing; run the host schema migration first")

    try:
        with _db_connection(read_only=False) as conn:
            conn.execute(
                """
                INSERT INTO tool_registry (
                    tool_name, project, source_module, source_class, source_method,
                    hook_point, return_type, known_side_effects, wrapper_path,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(tool_name, project) DO UPDATE SET
                    source_module = excluded.source_module,
                    source_class = excluded.source_class,
                    source_method = excluded.source_method,
                    hook_point = excluded.hook_point,
                    return_type = excluded.return_type,
                    known_side_effects = excluded.known_side_effects,
                    wrapper_path = excluded.wrapper_path,
                    created_by = excluded.created_by,
                    updated_at = datetime('now')
                """,
                _tool_registry_payload(body),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ?",
                (body.name, body.project),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc

    return {
        "registered": body.name,
        "project": body.project,
        "record": dict(row) if row else None,
        "note": "params_schema is accepted by the endpoint but not persisted in the current tool_registry schema",
    }
