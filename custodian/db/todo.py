from __future__ import annotations

import json
import re
import sqlite3
from mcp.types import TextContent

from custodian.db.connection import db_connection
from custodian.db.tasks import _next_task_id
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

def _next_todo_id(conn):
    """Generate the next race-safe todo ID."""
    max_num = conn.execute(
        "SELECT MAX(CAST(SUBSTR(todo_id, 4) AS INTEGER)) FROM todo_items WHERE todo_id LIKE 'TD-%'"
    ).fetchone()[0]
    next_num = (max_num or 0) + 1
    return f"TD-{next_num:03d}"

def _normalize_todo_id(todo_id):
    raw = str(todo_id or "").strip().upper()
    if not raw:
        return ""
    if re.fullmatch(r"TD-\d{3}", raw):
        return raw
    if re.fullmatch(r"TD-\d+", raw):
        return f"TD-{int(raw.split('-', 1)[1]):03d}"
    return ""

def _normalize_todo_priority(priority):
    value = str(priority or "medium").strip().lower() or "medium"
    if value not in {"low", "medium", "high"}:
        raise ValueError("priority must be one of: low, medium, high")
    return value

def _normalize_todo_status(status):
    value = str(status or "").strip().lower()
    if value not in {"open", "done", "promoted"}:
        raise ValueError("status must be one of: open, done, promoted")
    return value

def _get_todo_row(conn, todo_id):
    return conn.execute(
        "SELECT todo_id, project, title, description, priority, status, promoted_to, created_at, updated_at FROM todo_items WHERE todo_id = ?",
        (todo_id,),
    ).fetchone()

def _serialize_todo_row(row):
    return {
        "todo_id": row["todo_id"],
        "project": row["project"],
        "title": row["title"],
        "description": row["description"],
        "priority": row["priority"],
        "status": row["status"],
        "promoted_to": row["promoted_to"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

async def handle_add_todo(args):
    title = str(args.get("title") or "").strip()
    project_name = str(args.get("project") or "").strip()
    description = str(args.get("description") or "").strip() or None
    log_query("add_todo", project_name or None, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]

    try:
        priority = _normalize_todo_priority(args.get("priority", "medium"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]
            project_name = project["name"]

        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                todo_id = _next_todo_id(conn)
                conn.execute(
                    """
                    INSERT INTO todo_items (todo_id, project, title, description, priority, status)
                    VALUES (?, ?, ?, ?, ?, 'open')
                    """,
                    (todo_id, project_name or None, title, description, priority),
                )
                row = _get_todo_row(conn, todo_id)
                conn.commit()
                return [TextContent(type="text", text=json.dumps({
                    "todo_id": row["todo_id"],
                    "title": row["title"],
                    "project": row["project"],
                }, indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

    return [TextContent(type="text", text="Error: could not generate a unique TD-ID. Please try again.")]

async def handle_list_todos(args):
    project_name = str(args.get("project") or "").strip()
    system_only = args.get("system_only", False)
    include_all_statuses = args.get("include_all_statuses", False)
    log_query("list_todos", project_name or None, args)

    if not isinstance(system_only, bool):
        system_only = str(system_only).strip().lower() in {"1", "true", "yes"}
    if not isinstance(include_all_statuses, bool):
        include_all_statuses = str(include_all_statuses).strip().lower() in {"1", "true", "yes"}

    status = str(args.get("status") or "").strip().lower()
    try:
        if status:
            status = _normalize_todo_status(status)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    query = """
        SELECT todo_id, project, title, description, priority, status, promoted_to, created_at, updated_at
        FROM todo_items
        WHERE 1=1
    """
    params = []

    with db_connection() as conn:
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]
            project_name = project["name"]
            query += " AND project = ?"
            params.append(project_name)
        elif system_only:
            query += " AND project IS NULL"

        if include_all_statuses:
            pass
        elif status:
            query += " AND status = ?"
            params.append(status)
        else:
            query += " AND status = 'open'"

        query += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at DESC"
        rows = conn.execute(query, params).fetchall()

    return [TextContent(type="text", text=json.dumps([_serialize_todo_row(row) for row in rows], indent=2))]

async def handle_complete_todo(args):
    todo_id = _normalize_todo_id(args.get("todo_id", ""))
    log_query("complete_todo", None, {"todo_id": todo_id})

    if not todo_id:
        return [TextContent(type="text", text="Error: 'todo_id' is required.")]

    with db_connection() as conn:
        row = _get_todo_row(conn, todo_id)
        if not row:
            return [TextContent(type="text", text=f"Todo '{todo_id}' not found.")]

        conn.execute(
            "UPDATE todo_items SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE todo_id = ?",
            (todo_id,),
        )
        conn.commit()
        updated = _get_todo_row(conn, todo_id)

    return [TextContent(type="text", text=json.dumps(_serialize_todo_row(updated), indent=2))]

async def handle_promote_todo(args):
    todo_id = _normalize_todo_id(args.get("todo_id", ""))
    task_body = args.get("task_body", "")
    task_project = str(args.get("task_project") or "").strip()
    log_query("promote_todo", task_project or None, {"todo_id": todo_id, "task_project": task_project or None})

    if not todo_id:
        return [TextContent(type="text", text="Error: 'todo_id' is required.")]
    if not task_body:
        return [TextContent(type="text", text="Error: 'task_body' is required.")]

    with db_connection() as conn:
        row = _get_todo_row(conn, todo_id)
        if not row:
            return [TextContent(type="text", text=f"Todo '{todo_id}' not found.")]

        resolved_project = task_project or str(row["project"] or "").strip()
        task_prefix = "CT"
        if resolved_project:
            project = get_project_by_name(conn, resolved_project)
            if not project:
                return [TextContent(type="text", text=f"Project '{resolved_project}' not found. Use list_projects to see available projects.")]
            resolved_project = project["name"]
            task_prefix = (project["task_prefix"] or "").strip()
            if not task_prefix:
                return [TextContent(
                    type="text",
                    text=(
                        f"Project '{resolved_project}' does not have a task prefix yet. "
                        "Restart the Custodian service to run prefix assignment, then try again."
                    ),
                )]

        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                ct_id = _next_task_id(conn, task_prefix)
                conn.execute(
                    "INSERT INTO tasks (ct_id, title, body, project, created_by) VALUES (?, ?, ?, ?, ?)",
                    (ct_id, row["title"], task_body, resolved_project or None, "promote_todo"),
                )
                conn.execute(
                    "UPDATE todo_items SET status = 'promoted', promoted_to = ?, updated_at = CURRENT_TIMESTAMP WHERE todo_id = ?",
                    (ct_id, todo_id),
                )
                conn.commit()
                return [TextContent(type="text", text=json.dumps({
                    "todo_id": todo_id,
                    "promoted_to": ct_id,
                    "status": "promoted",
                }, indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

    return [TextContent(type="text", text=f"Error: could not generate a unique {task_prefix}-ID. Please try again.")]

async def handle_remove_todo(args):
    todo_id = _normalize_todo_id(args.get("todo_id", ""))
    log_query("remove_todo", None, {"todo_id": todo_id})

    if not todo_id:
        return [TextContent(type="text", text="Error: 'todo_id' is required.")]

    with db_connection() as conn:
        row = _get_todo_row(conn, todo_id)
        if not row:
            return [TextContent(type="text", text=f"Todo '{todo_id}' not found.")]
        conn.execute("DELETE FROM todo_items WHERE todo_id = ?", (todo_id,))
        conn.commit()

    return [TextContent(type="text", text=json.dumps({"todo_id": todo_id, "status": "removed"}, indent=2))]


def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def add_todo(conn, **params):
    return _unwrap(await handle_add_todo(params))


async def list_todos(conn, **params):
    return _unwrap(await handle_list_todos(params))


async def complete_todo(conn, **params):
    return _unwrap(await handle_complete_todo(params))


async def promote_todo(conn, **params):
    return _unwrap(await handle_promote_todo(params))


async def remove_todo(conn, **params):
    return _unwrap(await handle_remove_todo(params))
