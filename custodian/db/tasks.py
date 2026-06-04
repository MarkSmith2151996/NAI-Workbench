from __future__ import annotations

import json
import sqlite3
from mcp.types import TextContent

from custodian.db.connection import db_connection
from custodian.db.system import log_query

def get_project_by_name(conn, name):
    """Look up a project by name (case-insensitive, partial match)."""
    # Try exact match first
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try case-insensitive
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try partial match
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'",
        (f"%{name}%",),
    ).fetchone()
    return row

def _parse_stored_json_array(value):
    """Return a stored JSON array or [] for null/empty/malformed values."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []

def _normalize_string_list(value, field_name):
    """Validate an optional string array argument."""
    if value in (None, ""):
        return []

    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"'{field_name}' must be a JSON array of strings.") from exc

    if not isinstance(parsed, list):
        raise ValueError(f"'{field_name}' must be an array of strings.")

    normalized = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, str):
            raise ValueError(f"'{field_name}' entry {index} must be a string.")
        text = item.strip()
        if not text:
            raise ValueError(f"'{field_name}' entry {index} must not be empty.")
        normalized.append(text)

    return normalized

def _normalize_produced_files(produced_files):
    """Validate produced_files input and fill in sizes when files exist locally."""
    if produced_files in (None, ""):
        return []

    parsed = produced_files
    if isinstance(produced_files, str):
        try:
            parsed = json.loads(produced_files)
        except json.JSONDecodeError as exc:
            raise ValueError(f"produced_files must be valid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError("produced_files must be a JSON array.")

    normalized = []
    for index, entry in enumerate(parsed, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"produced_files entry {index} must be an object.")

        path = str(entry.get("path", "")).strip()
        if not path:
            raise ValueError(f"produced_files entry {index} is missing required field 'path'.")

        item = {"path": path}
        description = entry.get("description")
        if description is not None:
            description = str(description).strip()
            if description:
                item["description"] = description

        size = entry.get("size")
        if size is not None:
            try:
                item["size"] = int(size)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"produced_files entry {index} has invalid 'size': {size!r}"
                ) from exc

        normalized.append(item)

    return normalized

def _insert_session_update_row(
    conn,
    *,
    project_name,
    task_name,
    files_modified,
    unexecuted_steps=None,
    decisions=None,
    unfinished="",
    tokens_used=None,
    source="opencode",
):
    """Insert a session_updates row using the shared tool write path."""
    project = get_project_by_name(conn, project_name)
    if not project:
        raise ValueError(f"Project '{project_name}' not found. Use list_projects to see available projects.")

    cursor = conn.execute(
        """
        INSERT INTO session_updates (
            project_id, task, files_modified, unexecuted_steps,
            decisions, unfinished, tokens_used, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project["id"],
            task_name,
            json.dumps(files_modified or []),
            json.dumps(unexecuted_steps or []),
            json.dumps(decisions or []),
            unfinished or "",
            tokens_used,
            source,
        ),
    )
    return cursor.lastrowid, project["name"]

def _next_task_id(conn, prefix):
    """Generate the next race-safe task ID for a prefix."""
    start_index = len(prefix) + 2
    max_num = conn.execute(
        "SELECT MAX(CAST(SUBSTR(ct_id, ?) AS INTEGER)) FROM tasks WHERE ct_id LIKE ?",
        (start_index, f"{prefix}-%"),
    ).fetchone()[0]
    next_num = (max_num or 0) + 1
    return f"{prefix}-{next_num:03d}"

def _normalize_ct_id(ct_id):
    """Normalize user-provided CT IDs to uppercase trimmed form."""
    return str(ct_id or "").strip().upper()

def _format_task_body_response(row):
    """Return task metadata header plus body text."""
    metadata = {
        "ct_id": row["ct_id"],
        "title": row["title"],
        "project": row["project"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "executed_at": row["executed_at"],
        "execution_notes": row["execution_notes"],
        "produced_files": _parse_stored_json_array(row["produced_files"]),
    }
    if row["status"] == "executed":
        metadata["warning"] = f"This task has already been executed on {row['executed_at']}."

    return (
        "TASK_METADATA:\n"
        + json.dumps(metadata, indent=2)
        + "\n\nTASK_BODY:\n"
        + row["body"]
    )

async def handle_update_session_state(args):
    project_name = args["project"]

    log_query("update_session_state", project_name, args)

    try:
        with db_connection() as conn:
            update_id, resolved_project_name = _insert_session_update_row(
                conn,
                project_name=project_name,
                task_name=args.get("task", ""),
                files_modified=args.get("files_modified", []),
                unexecuted_steps=args.get("unexecuted_steps", []),
                decisions=args.get("decisions", []),
                unfinished=args.get("unfinished", ""),
                tokens_used=args.get("tokens_used"),
                source=args.get("source", "opencode"),
            )
            conn.commit()
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    result = {
        "status": "recorded",
        "update_id": update_id,
        "project": resolved_project_name,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_submit_task(args):
    title = args.get("title", "").strip()
    body = args.get("body", "")
    project_name = args.get("project", "")
    created_by = args.get("created_by", "claude")

    log_query("submit_task", project_name or None, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]
    if not body:
        return [TextContent(type="text", text="Error: 'body' is required.")]

    with db_connection() as conn:
        task_prefix = "CT"
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]
            project_name = project["name"]
            task_prefix = (project["task_prefix"] or "").strip()
            if not task_prefix:
                return [TextContent(
                    type="text",
                    text=(
                        f"Project '{project_name}' does not have a task prefix yet. "
                        "Restart the Custodian service to run prefix assignment, then try again."
                    ),
                )]

        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                ct_id = _next_task_id(conn, task_prefix)
                conn.execute(
                    """
                    INSERT INTO tasks (ct_id, title, body, project, created_by)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ct_id, title, body, project_name or None, created_by),
                )
                row = conn.execute(
                    "SELECT ct_id, title, status, created_at FROM tasks WHERE ct_id = ?",
                    (ct_id,),
                ).fetchone()
                conn.commit()
                result = {
                    "ct_id": row["ct_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

        return [TextContent(type="text", text=f"Error: could not generate a unique {task_prefix}-ID. Please try again.")]

async def handle_get_task(args):
    ct_id = _normalize_ct_id(args.get("ct_id", ""))

    if not ct_id:
        return [TextContent(type="text", text="Error: 'ct_id' is required.")]

    with db_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE ct_id = ?", (ct_id,)).fetchone()
        if not row:
            return [TextContent(type="text", text=f"Task '{ct_id}' not found.")]

    return [TextContent(type="text", text=_format_task_body_response(row))]

async def handle_list_tasks(args):
    status = str(args.get("status") or "").strip()
    project_name = args.get("project", "")
    limit = max(1, min(int(args.get("limit", 20)), 100))

    query = "SELECT ct_id, title, status, project, created_at, executed_at FROM tasks WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if project_name:
        query += " AND project = ?"
        params.append(project_name)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    result = [dict(row) for row in rows]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_mark_task_executed(args):
    ct_id = _normalize_ct_id(args.get("ct_id", ""))
    notes = args.get("notes", "")
    project_name = str(args.get("project") or "").strip()
    files_modified_provided = "files_modified" in args
    produced_files = args.get("produced_files")
    log_query(
        "mark_task_executed",
        None,
        {
            "ct_id": ct_id,
            "notes": notes,
            "project": project_name or None,
            "files_modified": args.get("files_modified") if files_modified_provided else None,
            "unexecuted_steps": args.get("unexecuted_steps"),
            "decisions": args.get("decisions"),
            "unfinished": args.get("unfinished"),
            "tokens_used": args.get("tokens_used"),
            "produced_files": produced_files,
        },
    )

    if not ct_id:
        return [TextContent(type="text", text="Error: 'ct_id' is required.")]

    try:
        normalized_files = _normalize_produced_files(produced_files)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        task_row = conn.execute(
            "SELECT ct_id, title, project FROM tasks WHERE ct_id = ?",
            (ct_id,),
        ).fetchone()
        if not task_row:
            return [TextContent(type="text", text=f"Task '{ct_id}' not found.")]

        update_id = None
        resolved_project_name = None

        if files_modified_provided:
            resolved_project_name = project_name or str(task_row["project"] or "").strip()
            if not resolved_project_name:
                return [TextContent(
                    type="text",
                    text="Error: session update requested but no project was provided and the task has no project.",
                )]

        cursor = conn.execute(
            """
            UPDATE tasks
            SET status = 'executed', executed_at = datetime('now'), execution_notes = ?, produced_files = ?
            WHERE ct_id = ?
            """,
            (
                notes or None,
                json.dumps(normalized_files) if normalized_files else None,
                ct_id,
            ),
        )

        if files_modified_provided:
            try:
                update_id, resolved_project_name = _insert_session_update_row(
                    conn,
                    project_name=resolved_project_name,
                    task_name=task_row["title"],
                    files_modified=args.get("files_modified", []),
                    unexecuted_steps=args.get("unexecuted_steps", []),
                    decisions=args.get("decisions", []),
                    unfinished=args.get("unfinished", ""),
                    tokens_used=args.get("tokens_used"),
                    source=args.get("source", "opencode"),
                )
            except ValueError as exc:
                conn.rollback()
                return [TextContent(type="text", text=f"Error: {exc}")]

        conn.commit()

    result = {
        "ct_id": ct_id,
        "status": "executed",
        "execution_notes": notes,
        "produced_files_recorded": len(normalized_files),
    }
    if update_id is not None:
        result["session_update_recorded"] = True
        result["session_update_id"] = update_id
        result["project"] = resolved_project_name
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def update_session_state(conn, **params):
    return _unwrap(await handle_update_session_state(params))


async def submit_task(conn, **params):
    return _unwrap(await handle_submit_task(params))


async def get_task(conn, **params):
    return _unwrap(await handle_get_task(params))


async def list_tasks(conn, **params):
    return _unwrap(await handle_list_tasks(params))


async def mark_task_executed(conn, **params):
    return _unwrap(await handle_mark_task_executed(params))
