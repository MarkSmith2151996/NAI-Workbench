from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from mcp.types import TextContent

from custodian.db.connection import db_connection
from custodian.db.tasks import _parse_stored_json_array
from custodian.db.system import log_query

MEMORY_PREVIEW_CHARS = 2000

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

def _normalize_memory_flag_status(status, *, allow_open=True):
    """Validate a memory drift flag status value."""
    value = str(status or "open").strip().lower()
    allowed = {"open", "resolved", "wontfix"} if allow_open else {"resolved", "wontfix"}
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"'status' must be one of: {options}.")
    return value

def _normalize_meta_record_id(value, prefix):
    """Normalize and validate a meta-log record ID."""
    record_id = str(value or "").strip().upper()
    if not record_id:
        raise ValueError("'id' is required.")
    if not re.fullmatch(rf"{prefix}-\d{{3}}", record_id):
        raise ValueError(f"ID must match {prefix}-NNN.")
    return record_id

def _serialize_memory_flag_row(row):
    """Convert a memory_flags row joined to memories into a JSON-friendly dict."""
    result = {
        "id": row["id"],
        "memory_id": row["memory_id"],
        "reason": row["reason"],
        "flagged_in_context": row["flagged_in_context"],
        "status": row["status"],
        "resolved_by": row["resolved_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "memory_content" in row.keys():
        result.update(
            {
                "memory_content": row["memory_content"],
                "memory_tags": _parse_stored_json_array(row["memory_tags"]),
                "memory_project": row["memory_project"],
                "memory_importance": row["memory_importance"],
                "memory_updated_at": row["memory_updated_at"],
            }
        )
    return result

def _bump_access(conn, memory_ids):
    """Increment access_count for retrieved memories."""
    if not memory_ids:
        return
    placeholders = ",".join("?" for _ in memory_ids)
    conn.execute(
        f"UPDATE memories SET access_count = access_count + 1 WHERE id IN ({placeholders})",
        memory_ids,
    )
    conn.commit()

def _format_memory(row):
    """Format a memory row for display."""
    tags = row["tags"] or "[]"
    project = row["project_name"] if "project_name" in row.keys() else None
    proj_str = f" [{project}]" if project else ""
    return (
        f"  [{row['id']}] (importance: {row['importance']}, "
        f"accessed: {row['access_count']}x){proj_str}\n"
        f"      Tags: {tags}\n"
        f"      {row['content'][:MEMORY_PREVIEW_CHARS]}"
        f"{'...' if len(row['content']) > MEMORY_PREVIEW_CHARS else ''}"
    )

async def handle_memory_store(args):
    """Save a new memory."""
    content = args.get("content", "").strip()
    if not content:
        return [TextContent(type="text", text="Error: 'content' is required.")]

    tags = json.dumps(args.get("tags", []))
    project_name = args.get("project", "")
    importance = max(1, min(10, args.get("importance", 5)))
    log_query("memory_store", project_name, args)

    with db_connection() as conn:
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Error: project '{project_name}' not found.")]
            project_id = project["id"]

        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO memories (content, tags, project_id, source, importance, created_at, updated_at)
               VALUES (?, ?, ?, 'mcp', ?, ?, ?)""",
            (content, tags, project_id, importance, now, now),
        )
        conn.commit()
        memory_id = cursor.lastrowid

    return [TextContent(
        type="text",
        text=f"Memory #{memory_id} stored (importance: {importance}, tags: {tags}).",
    )]

async def handle_memory_search(args):
    """Search memories with FTS5 or filters."""
    query = args.get("query", "").strip()
    project_name = args.get("project", "")
    filter_tags = args.get("tags", [])
    limit = min(args.get("limit", 20), 100)

    with db_connection() as conn:
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if project:
                project_id = project["id"]

        if query:
            # FTS5 search with bm25 ranking
            sql = """
                SELECT m.*, p.name as project_name, bm25(memories_fts) as rank
                FROM memories_fts fts
                JOIN memories m ON m.id = fts.rowid
                LEFT JOIN projects p ON p.id = m.project_id
                WHERE memories_fts MATCH ?
            """
            params = [query]

            if project_id is not None:
                sql += " AND (m.project_id = ? OR m.project_id IS NULL)"
                params.append(project_id)

            if filter_tags:
                for tag in filter_tags:
                    sql += " AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value = ?)"
                    params.append(tag)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
        else:
            # No query — filter only
            sql = """
                SELECT m.*, p.name as project_name
                FROM memories m
                LEFT JOIN projects p ON p.id = m.project_id
                WHERE 1=1
            """
            params = []

            if project_id is not None:
                sql += " AND (m.project_id = ? OR m.project_id IS NULL)"
                params.append(project_id)

            if filter_tags:
                for tag in filter_tags:
                    sql += " AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value = ?)"
                    params.append(tag)

            sql += " ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?"
            params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        if not rows:
            return [TextContent(type="text", text="No memories found.")]

        ids = [r["id"] for r in rows]
        _bump_access(conn, ids)

    lines = [f"Found {len(rows)} memory/memories:\n"]
    for r in rows:
        lines.append(_format_memory(r))
    return [TextContent(type="text", text="\n".join(lines))]

async def handle_memory_get(args):
    """Retrieve a single memory row with full untruncated content."""
    memory_id = args.get("id")
    if memory_id is None:
        return [TextContent(type="text", text="Error: 'id' is required.")]

    with db_connection() as conn:
        row = conn.execute(
            """SELECT m.*, p.name as project_name FROM memories m
               LEFT JOIN projects p ON p.id = m.project_id
               WHERE m.id = ?""",
            (memory_id,),
        ).fetchone()
        if not row:
            return [TextContent(type="text", text=f"Memory #{memory_id} not found.")]

        _bump_access(conn, [memory_id])

    result = {
        "id": row["id"],
        "content": row["content"],
        "tags": json.loads(row["tags"] or "[]"),
        "project": row["project_name"],
        "importance": row["importance"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "access_count": (row["access_count"] or 0) + 1,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_memory_list(args):
    """Browse all memories with pagination."""
    project_name = args.get("project", "")
    limit = min(args.get("limit", 20), 100)
    offset = max(args.get("offset", 0), 0)

    with db_connection() as conn:
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if project:
                project_id = project["id"]

        if project_id is not None:
            rows = conn.execute(
                """SELECT m.*, p.name as project_name FROM memories m
                   LEFT JOIN projects p ON p.id = m.project_id
                   WHERE m.project_id = ? OR m.project_id IS NULL
                   ORDER BY m.importance DESC, m.updated_at DESC
                   LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE project_id = ? OR project_id IS NULL",
                (project_id,),
            ).fetchone()[0]
        else:
            rows = conn.execute(
                """SELECT m.*, p.name as project_name FROM memories m
                   LEFT JOIN projects p ON p.id = m.project_id
                   ORDER BY m.importance DESC, m.updated_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    if not rows:
        return [TextContent(type="text", text="No memories found.")]

    lines = [f"Memories ({offset+1}-{offset+len(rows)} of {total}):\n"]
    for r in rows:
        lines.append(_format_memory(r))
    return [TextContent(type="text", text="\n".join(lines))]

async def handle_memory_update(args):
    """Update a memory by ID."""
    memory_id = args.get("id")
    if memory_id is None:
        return [TextContent(type="text", text="Error: 'id' is required.")]
    log_query("memory_update", None, args)

    with db_connection() as conn:
        existing = conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not existing:
            return [TextContent(type="text", text=f"Memory #{memory_id} not found.")]

        updates = []
        params = []

        if "content" in args and args["content"]:
            updates.append("content = ?")
            params.append(args["content"].strip())
        if "tags" in args:
            updates.append("tags = ?")
            params.append(json.dumps(args["tags"]))
        if "importance" in args:
            updates.append("importance = ?")
            params.append(max(1, min(10, args["importance"])))

        if not updates:
            return [TextContent(type="text", text="Nothing to update — pass content, tags, or importance.")]

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(memory_id)

        conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()

    return [TextContent(type="text", text=f"Memory #{memory_id} updated.")]

async def handle_memory_delete(args):
    """Delete a memory by ID."""
    memory_id = args.get("id")
    if memory_id is None:
        return [TextContent(type="text", text="Error: 'id' is required.")]
    log_query("memory_delete", None, args)

    with db_connection() as conn:
        existing = conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not existing:
            return [TextContent(type="text", text=f"Memory #{memory_id} not found.")]

        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()

    return [TextContent(type="text", text=f"Memory #{memory_id} deleted.")]

async def handle_memory_context(args):
    """Load relevant memories for session context. 3-pass merge: high-importance, recent, topic-matched."""
    project_name = args.get("project", "")
    topics = args.get("topics", [])
    limit = min(args.get("limit", 30), 100)

    seen_ids = set()
    results = []

    with db_connection() as conn:
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if project:
                project_id = project["id"]

        project_filter = ""
        project_params = ()
        if project_id is not None:
            project_filter = "AND (m.project_id = ? OR m.project_id IS NULL)"
            project_params = (project_id,)

        # Pass 1: High-importance memories (>= 7)
        rows = conn.execute(
            f"""SELECT m.*, p.name as project_name FROM memories m
                LEFT JOIN projects p ON p.id = m.project_id
                WHERE m.importance >= 7 {project_filter}
                ORDER BY m.importance DESC, m.updated_at DESC
                LIMIT ?""",
            (*project_params, limit),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                results.append(r)

        # Pass 2: Recently accessed
        remaining = limit - len(results)
        if remaining > 0:
            rows = conn.execute(
                f"""SELECT m.*, p.name as project_name FROM memories m
                    LEFT JOIN projects p ON p.id = m.project_id
                    WHERE m.access_count > 0 {project_filter}
                    ORDER BY m.updated_at DESC
                    LIMIT ?""",
                (*project_params, remaining),
            ).fetchall()
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    results.append(r)

        # Pass 3: Topic FTS match
        remaining = limit - len(results)
        if remaining > 0 and topics:
            topic_query = " OR ".join(topics)
            try:
                rows = conn.execute(
                    f"""SELECT m.*, p.name as project_name, bm25(memories_fts) as rank
                        FROM memories_fts fts
                        JOIN memories m ON m.id = fts.rowid
                        LEFT JOIN projects p ON p.id = m.project_id
                        WHERE memories_fts MATCH ?
                        {project_filter.replace('m.project_id', 'm.project_id')}
                        ORDER BY rank LIMIT ?""",
                    (topic_query, *project_params, remaining),
                ).fetchall()
                for r in rows:
                    if r["id"] not in seen_ids:
                        seen_ids.add(r["id"])
                        results.append(r)
            except Exception:
                pass  # FTS query syntax error — skip topic matching

        # Bump access counts
        all_ids = [r["id"] for r in results]
        _bump_access(conn, all_ids)

    if not results:
        return [TextContent(type="text", text="No memories found for this context.")]

    lines = [f"Session context — {len(results)} memories loaded:\n"]
    for r in results:
        lines.append(_format_memory(r))
    return [TextContent(type="text", text="\n".join(lines))]

async def handle_flag_memory_drift(args):
    """Create an MF-NNN flag when a stored memory has drifted."""
    log_query("flag_memory_drift", None, args)

    try:
        memory_id = int(args.get("memory_id"))
    except (TypeError, ValueError):
        return [TextContent(type="text", text="Error: 'memory_id' is required and must be an integer.")]

    reason = str(args.get("reason") or "").strip()
    flagged_in_context = str(args.get("flagged_in_context") or "").strip()

    if not reason:
        return [TextContent(type="text", text="Error: 'reason' is required.")]
    if not flagged_in_context:
        return [TextContent(type="text", text="Error: 'flagged_in_context' is required.")]

    with db_connection() as conn:
        memory = conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not memory:
            return [TextContent(type="text", text=f"Memory #{memory_id} not found.")]

        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                flag_id = _next_meta_id(conn, "memory_flags", "MF")
                conn.execute(
                    """
                    INSERT INTO memory_flags (id, memory_id, reason, flagged_in_context)
                    VALUES (?, ?, ?, ?)
                    """,
                    (flag_id, memory_id, reason, flagged_in_context),
                )
                row = conn.execute(
                    "SELECT id, memory_id, status FROM memory_flags WHERE id = ?",
                    (flag_id,),
                ).fetchone()
                conn.commit()
                return [TextContent(type="text", text=json.dumps(dict(row), indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

    return [TextContent(type="text", text="Error: could not generate a unique MF-ID. Please try again.")]

async def handle_list_memory_flags(args):
    """List memory drift flags, joined to current memory content."""
    try:
        status = _normalize_memory_flag_status(args.get("status", "open"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    memory_id = args.get("memory_id")
    if memory_id not in (None, ""):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return [TextContent(type="text", text="Error: 'memory_id' must be an integer.")]
    else:
        memory_id = None

    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        return [TextContent(type="text", text="Error: 'limit' must be an integer.")]
    limit = max(1, min(limit, 100))

    sql = """
        SELECT
            mf.*,
            m.content AS memory_content,
            m.tags AS memory_tags,
            m.importance AS memory_importance,
            m.updated_at AS memory_updated_at,
            p.name AS memory_project
        FROM memory_flags mf
        JOIN memories m ON m.id = mf.memory_id
        LEFT JOIN projects p ON p.id = m.project_id
        WHERE mf.status = ?
    """
    params = [status]
    if memory_id is not None:
        sql += " AND mf.memory_id = ?"
        params.append(memory_id)
    sql += " ORDER BY mf.created_at DESC LIMIT ?"
    params.append(limit)

    with db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = [_serialize_memory_flag_row(row) for row in rows]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_resolve_memory_flag(args):
    """Resolve or wontfix a memory drift flag after audit."""
    log_query("resolve_memory_flag", None, args)

    try:
        flag_id = _normalize_meta_record_id(args.get("id"), "MF")
        status = _normalize_memory_flag_status(args.get("status"), allow_open=False)
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    resolved_by = str(args.get("resolved_by") or "").strip()
    if not resolved_by:
        return [TextContent(type="text", text="Error: 'resolved_by' is required.")]

    with db_connection() as conn:
        existing = conn.execute("SELECT id FROM memory_flags WHERE id = ?", (flag_id,)).fetchone()
        if not existing:
            return [TextContent(type="text", text=f"Memory flag '{flag_id}' not found.")]

        conn.execute(
            """
            UPDATE memory_flags
            SET status = ?,
                resolved_by = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ?
            """,
            (status, resolved_by, flag_id),
        )
        row = conn.execute(
            """
            SELECT
                mf.*,
                m.content AS memory_content,
                m.tags AS memory_tags,
                m.importance AS memory_importance,
                m.updated_at AS memory_updated_at,
                p.name AS memory_project
            FROM memory_flags mf
            JOIN memories m ON m.id = mf.memory_id
            LEFT JOIN projects p ON p.id = m.project_id
            WHERE mf.id = ?
            """,
            (flag_id,),
        ).fetchone()
        conn.commit()

    return [TextContent(type="text", text=json.dumps(_serialize_memory_flag_row(row), indent=2))]


def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def memory_store(conn, **params):
    return _unwrap(await handle_memory_store(params))


async def memory_search(conn, **params):
    return _unwrap(await handle_memory_search(params))


async def memory_get(conn, **params):
    return _unwrap(await handle_memory_get(params))


async def memory_list(conn, **params):
    return _unwrap(await handle_memory_list(params))


async def memory_update(conn, **params):
    return _unwrap(await handle_memory_update(params))


async def memory_delete(conn, **params):
    return _unwrap(await handle_memory_delete(params))


async def memory_context(conn, **params):
    return _unwrap(await handle_memory_context(params))


async def flag_memory_drift(conn, **params):
    return _unwrap(await handle_flag_memory_drift(params))


async def list_memory_flags(conn, **params):
    return _unwrap(await handle_list_memory_flags(params))


async def resolve_memory_flag(conn, **params):
    return _unwrap(await handle_resolve_memory_flag(params))
