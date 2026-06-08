from __future__ import annotations

import json
import sqlite3

from mcp.types import TextContent

from custodian.db.connection import db_connection
from custodian.db.system import log_query


def _normalize_cs_id(cs_id: str) -> str:
    """Normalize user-provided CS IDs to uppercase trimmed form."""
    return str(cs_id or "").strip().upper()


def _next_transport_id(conn: sqlite3.Connection) -> str:
    """Generate the next race-safe transport ID."""
    max_id = conn.execute("SELECT MAX(id) FROM session_transports").fetchone()[0]
    return f"CS-{(max_id or 0) + 1:03d}"


async def handle_submit_transport(args: dict) -> list[TextContent]:
    title = str(args.get("title") or "").strip()
    body = args.get("body") or ""
    source_project = str(args.get("source_project") or "").strip() or None
    target_project = str(args.get("target_project") or "").strip() or None
    created_by = str(args.get("created_by") or "claude").strip() or "claude"

    log_query("submit_transport", source_project, args)

    if not title:
        return [TextContent(type="text", text="Error: 'title' is required.")]
    if not body:
        return [TextContent(type="text", text="Error: 'body' is required.")]

    with db_connection() as conn:
        for _attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                cs_id = _next_transport_id(conn)
                conn.execute(
                    """
                    INSERT INTO session_transports (
                        cs_id, title, body, source_project, target_project, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (cs_id, title, body, source_project, target_project, created_by),
                )
                row = conn.execute(
                    """
                    SELECT cs_id, title, source_project, target_project, created_at
                    FROM session_transports
                    WHERE cs_id = ?
                    """,
                    (cs_id,),
                ).fetchone()
                conn.commit()
                result = dict(row)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except sqlite3.IntegrityError:
                conn.rollback()
                continue

        return [TextContent(type="text", text="Error: could not generate a unique CS-ID. Please try again.")]


async def handle_get_transport(args: dict) -> list[TextContent]:
    cs_id = _normalize_cs_id(args.get("cs_id", ""))

    if not cs_id:
        return [TextContent(type="text", text="Error: 'cs_id' is required.")]

    log_query("get_transport", None, args)

    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM session_transports WHERE cs_id = ?",
            (cs_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return [TextContent(type="text", text=json.dumps({"error": f"Transport {cs_id} not found"}, indent=2))]

        previously_pulled = bool(row["pulled_at"])
        if not previously_pulled:
            conn.execute(
                """
                UPDATE session_transports
                SET pulled_at = datetime('now'), pulled_by = ?
                WHERE cs_id = ?
                """,
                (str(args.get("pulled_by") or "claude"), cs_id),
            )
            row = conn.execute(
                "SELECT * FROM session_transports WHERE cs_id = ?",
                (cs_id,),
            ).fetchone()
        conn.commit()

    result = {
        "cs_id": row["cs_id"],
        "title": row["title"],
        "body": row["body"],
        "source_project": row["source_project"],
        "target_project": row["target_project"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "pulled_at": row["pulled_at"],
        "previously_pulled": previously_pulled,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_list_transports(args: dict) -> list[TextContent]:
    limit = max(1, min(int(args.get("limit", 10)), 100))
    project = str(args.get("project") or "").strip()
    status = str(args.get("status") or "").strip().lower()

    log_query("list_transports", project or None, args)

    query = """
        SELECT cs_id, title, source_project, target_project, created_at, pulled_at
        FROM session_transports
        WHERE 1=1
    """
    params: list[object] = []
    if project:
        query += " AND (source_project = ? OR target_project = ?)"
        params.extend([project, project])
    if status == "pending":
        query += " AND pulled_at IS NULL"
    elif status == "pulled":
        query += " AND pulled_at IS NOT NULL"
    elif status:
        return [TextContent(type="text", text="Error: 'status' must be 'pending' or 'pulled'.")]

    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with db_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    result = [dict(row) for row in rows]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _unwrap(result: list[TextContent]):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def submit_transport(conn, **params):
    return _unwrap(await handle_submit_transport(params))


async def get_transport(conn, **params):
    return _unwrap(await handle_get_transport(params))


async def list_transports(conn, **params):
    return _unwrap(await handle_list_transports(params))
