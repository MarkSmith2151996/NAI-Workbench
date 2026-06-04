from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from mcp.types import TextContent

from custodian.db.connection import db_connection
from custodian.db.tasks import _parse_stored_json_array
from custodian.db.system import log_query, normalize_since_input

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

def _normalize_friction_status(status):
    """Validate a friction point status value."""
    value = str(status or "open").strip().lower()
    allowed = {"open", "mitigated", "resolved", "wontfix"}
    if value not in allowed:
        raise ValueError(
            "'status' must be one of: open, mitigated, resolved, wontfix."
        )
    return value

def _normalize_meta_record_id(value, prefix):
    """Normalize and validate a meta-log record ID."""
    record_id = str(value or "").strip().upper()
    if not record_id:
        raise ValueError("'id' is required.")
    if not re.fullmatch(rf"{prefix}-\d{{3}}", record_id):
        raise ValueError(f"ID must match {prefix}-NNN.")
    return record_id

def _decode_optional_json_array(value):
    """Decode stored optional JSON arrays, preserving null as None."""
    if value is None:
        return None
    return _parse_stored_json_array(value)

def _serialize_friction_point_row(row):
    """Convert a friction_points row to a JSON-friendly dict."""
    return {
        "id": row["id"],
        "title": row["title"],
        "surface_event": row["surface_event"],
        "project_state_context": row["project_state_context"],
        "chat_session_context": row["chat_session_context"],
        "root_cause": row["root_cause"],
        "status": row["status"],
        "resolved_by": _decode_optional_json_array(row["resolved_by"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _serialize_changelog_entry_row(row):
    """Convert a changelog_entries row to a JSON-friendly dict."""
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "sub_items": _decode_optional_json_array(row["sub_items"]),
        "resolves_friction": _decode_optional_json_array(row["resolves_friction"]),
        "related_task_id": row["related_task_id"],
        "created_at": row["created_at"],
    }

def _next_meta_id(conn, table_name, prefix):
    """Generate the next race-safe meta-log ID for a supported table."""
    if table_name not in {"friction_points", "changelog_entries", "memory_flags"}:
        raise ValueError(f"Unsupported meta-log table: {table_name}")

    start_index = len(prefix) + 2
    max_num = conn.execute(
        f"SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) FROM {table_name} WHERE id LIKE ?",
        (start_index, f"{prefix}-%"),
    ).fetchone()[0]
    next_num = (max_num or 0) + 1
    return f"{prefix}-{next_num:03d}"

async def handle_add_system_update(args):
    title = args.get("title", "").strip()
    description = args.get("description", "").strip()
    category = args.get("category", "").strip()
    project = args.get("project", "").strip() or None
    created_by = args.get("created_by", "claude")

    log_query("add_system_update", project, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]
    if not description:
        return [TextContent(type="text", text="Error: 'description' is required.")]
    if not category:
        return [TextContent(type="text", text="Error: 'category' is required.")]

    with db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO system_updates (title, description, category, project, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title, description, category, project, created_by),
        )
        row = conn.execute(
            "SELECT id, title, category, created_at FROM system_updates WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        conn.commit()

    return [TextContent(type="text", text=json.dumps(dict(row), indent=2))]

async def handle_check_system_updates(args):
    since = args.get("since")
    since_hours = args.get("since_hours")
    category = args.get("category", "").strip() or None
    limit = max(1, min(int(args.get("limit", 20)), 100))

    try:
        cutoff = normalize_since_input(since, since_hours)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    query = (
        "SELECT id, title, description, category, project, created_at, created_by "
        "FROM system_updates WHERE created_at >= ?"
    )
    params = [cutoff]
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    updates = [dict(row) for row in rows]
    message = (
        f"Found {len(updates)} update(s) since {cutoff}."
        if updates else
        f"No system updates since {cutoff}."
    )
    result = {
        "count": len(updates),
        "updates": updates,
        "message": message,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_log_friction_point(args):
    title = str(args.get("title") or "").strip()
    surface_event = str(args.get("surface_event") or "").strip()
    project_state_context = str(args.get("project_state_context") or "").strip()
    chat_session_context = str(args.get("chat_session_context") or "").strip()
    root_cause = str(args.get("root_cause") or "").strip() or None

    log_query("log_friction_point", None, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]
    if not surface_event:
        return [TextContent(type="text", text="Error: 'surface_event' is required.")]
    if not project_state_context:
        return [TextContent(type="text", text="Error: 'project_state_context' is required.")]
    if not chat_session_context:
        return [TextContent(type="text", text="Error: 'chat_session_context' is required.")]

    try:
        status = _normalize_friction_status(args.get("status", "open"))
        resolved_by = _normalize_string_list(args.get("resolved_by"), "resolved_by")
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                friction_id = _next_meta_id(conn, "friction_points", "FP")
                conn.execute(
                    """
                    INSERT INTO friction_points (
                        id, title, surface_event, project_state_context,
                        chat_session_context, root_cause, status, resolved_by
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        friction_id,
                        title,
                        surface_event,
                        project_state_context,
                        chat_session_context,
                        root_cause,
                        status,
                        json.dumps(resolved_by) if args.get("resolved_by") is not None else None,
                    ),
                )
                row = conn.execute(
                    "SELECT id, created_at FROM friction_points WHERE id = ?",
                    (friction_id,),
                ).fetchone()
                conn.commit()
                return [TextContent(type="text", text=json.dumps(dict(row), indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

    return [TextContent(type="text", text="Error: could not generate a unique FP-ID. Please try again.")]

async def handle_log_changelog_entry(args):
    title = str(args.get("title") or "").strip()
    summary = str(args.get("summary") or "").strip()
    related_task_id = str(args.get("related_task_id") or "").strip() or None

    log_query("log_changelog_entry", None, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]
    if not summary:
        return [TextContent(type="text", text="Error: 'summary' is required.")]

    try:
        sub_items = _normalize_string_list(args.get("sub_items"), "sub_items")
        resolves_friction = _normalize_string_list(
            args.get("resolves_friction"), "resolves_friction"
        )
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")

                friction_rows = {}
                if resolves_friction:
                    placeholders = ", ".join("?" for _ in resolves_friction)
                    rows = conn.execute(
                        f"SELECT id, resolved_by FROM friction_points WHERE id IN ({placeholders})",
                        resolves_friction,
                    ).fetchall()
                    friction_rows = {row["id"]: row for row in rows}
                    missing = [fp_id for fp_id in resolves_friction if fp_id not in friction_rows]
                    if missing:
                        conn.rollback()
                        return [
                            TextContent(
                                type="text",
                                text=(
                                    "Error: resolves_friction references unknown friction point IDs: "
                                    + ", ".join(missing)
                                ),
                            )
                        ]

                changelog_id = _next_meta_id(conn, "changelog_entries", "CL")
                conn.execute(
                    """
                    INSERT INTO changelog_entries (
                        id, title, summary, sub_items, resolves_friction, related_task_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        changelog_id,
                        title,
                        summary,
                        json.dumps(sub_items) if args.get("sub_items") is not None else None,
                        json.dumps(resolves_friction)
                        if args.get("resolves_friction") is not None
                        else None,
                        related_task_id,
                    ),
                )

                for fp_id in resolves_friction:
                    current = _parse_stored_json_array(friction_rows[fp_id]["resolved_by"])
                    if changelog_id not in current:
                        current.append(changelog_id)
                    conn.execute(
                        """
                        UPDATE friction_points
                        SET resolved_by = ?,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                        WHERE id = ?
                        """,
                        (json.dumps(current), fp_id),
                    )

                row = conn.execute(
                    "SELECT id, created_at FROM changelog_entries WHERE id = ?",
                    (changelog_id,),
                ).fetchone()
                conn.commit()
                return [TextContent(type="text", text=json.dumps(dict(row), indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

    return [TextContent(type="text", text="Error: could not generate a unique CL-ID. Please try again.")]

async def handle_update_friction_status(args):
    log_query("update_friction_status", None, args)

    try:
        friction_id = _normalize_meta_record_id(args.get("id"), "FP")
        status = _normalize_friction_status(args.get("status"))
        resolved_by = (
            _normalize_string_list(args.get("resolved_by"), "resolved_by")
            if "resolved_by" in args else None
        )
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    root_cause = None
    if "root_cause" in args:
        root_cause = str(args.get("root_cause") or "").strip() or None

    with db_connection() as conn:
        row = conn.execute(
            "SELECT id FROM friction_points WHERE id = ?",
            (friction_id,),
        ).fetchone()
        if not row:
            return [TextContent(type="text", text=f"Friction point '{friction_id}' not found.")]

        fields = ["status = ?", "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"]
        params = [status]

        if "resolved_by" in args:
            fields.append("resolved_by = ?")
            params.append(json.dumps(resolved_by))
        if "root_cause" in args:
            fields.append("root_cause = ?")
            params.append(root_cause)

        params.append(friction_id)
        conn.execute(
            f"UPDATE friction_points SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        updated = conn.execute(
            "SELECT id, status, updated_at FROM friction_points WHERE id = ?",
            (friction_id,),
        ).fetchone()
        conn.commit()

    return [TextContent(type="text", text=json.dumps(dict(updated), indent=2))]

async def handle_get_meta_summary(args):
    with db_connection() as conn:
        counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_friction_count,
                SUM(CASE WHEN status = 'mitigated' THEN 1 ELSE 0 END) AS mitigated_friction_count,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved_friction_count,
                SUM(CASE WHEN status = 'wontfix' THEN 1 ELSE 0 END) AS wontfix_friction_count
            FROM friction_points
            """
        ).fetchone()
        total_changelog_count = conn.execute(
            "SELECT COUNT(*) FROM changelog_entries"
        ).fetchone()[0]
        recent_changes_rows = conn.execute(
            """
            SELECT id, title, created_at
            FROM changelog_entries
            ORDER BY created_at DESC
            LIMIT 5
            """
        ).fetchall()
        open_friction_rows = conn.execute(
            """
            SELECT id, title, created_at
            FROM friction_points
            WHERE status = 'open'
            ORDER BY created_at DESC
            """
        ).fetchall()

    result = {
        "open_friction_count": int(counts["open_friction_count"] or 0),
        "mitigated_friction_count": int(counts["mitigated_friction_count"] or 0),
        "resolved_friction_count": int(counts["resolved_friction_count"] or 0),
        "wontfix_friction_count": int(counts["wontfix_friction_count"] or 0),
        "total_changelog_count": int(total_changelog_count or 0),
        "recent_changes": [dict(row) for row in recent_changes_rows],
        "open_friction": [dict(row) for row in open_friction_rows],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_friction_point(args):
    try:
        friction_id = _normalize_meta_record_id(args.get("id"), "FP")
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM friction_points WHERE id = ?",
            (friction_id,),
        ).fetchone()

    if not row:
        return [TextContent(type="text", text=f"Friction point '{friction_id}' not found.")]

    return [
        TextContent(
            type="text",
            text=json.dumps(_serialize_friction_point_row(row), indent=2),
        )
    ]

async def handle_get_changelog_entry(args):
    try:
        changelog_id = _normalize_meta_record_id(args.get("id"), "CL")
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM changelog_entries WHERE id = ?",
            (changelog_id,),
        ).fetchone()

    if not row:
        return [TextContent(type="text", text=f"Changelog entry '{changelog_id}' not found.")]

    return [
        TextContent(
            type="text",
            text=json.dumps(_serialize_changelog_entry_row(row), indent=2),
        )
    ]


def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def log_friction_point(conn, **params):
    return _unwrap(await handle_log_friction_point(params))


async def log_changelog_entry(conn, **params):
    return _unwrap(await handle_log_changelog_entry(params))


async def update_friction_status(conn, **params):
    return _unwrap(await handle_update_friction_status(params))


async def get_meta_summary(conn, **params):
    return _unwrap(await handle_get_meta_summary(params))


async def get_friction_point(conn, **params):
    return _unwrap(await handle_get_friction_point(params))


async def get_changelog_entry(conn, **params):
    return _unwrap(await handle_get_changelog_entry(params))
