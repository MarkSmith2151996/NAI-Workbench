from __future__ import annotations

import json
import os
from datetime import datetime, timedelta


ROADMAP_FILE_PATH = "/mnt/c/Users/Big A/custodian-shared/nai-workbench/roadmap/roadmap.md"


def log_query(conn, tool_name: str, project_name: str | None = None, params: dict | None = None) -> None:
    try:
        conn.execute(
            "INSERT INTO query_log (tool_name, project_name, query_params) VALUES (?, ?, ?)",
            (tool_name, project_name, json.dumps(params) if params else None),
        )
        conn.commit()
    except Exception:
        pass


def get_config_value(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row[0])


def set_config_value(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO config(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
        (key, value),
    )
    conn.commit()


def normalize_since_input(since: str | None, since_hours: int | None) -> str:
    if since:
        raw = str(since).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    hours = since_hours if since_hours is not None else 168
    hours = max(1, int(hours))
    cutoff = datetime.now() - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def add_system_update(conn=None, title: str = "", description: str = "", category: str = "", project: str | None = None, created_by: str = "claude", **_kwargs) -> dict:
    if conn is None:
        from custodian.db.connection import db_connection

        with db_connection() as local_conn:
            return add_system_update(local_conn, title=title, description=description, category=category, project=project, created_by=created_by)
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
    return dict(row)


def check_system_updates(conn=None, since: str | None = None, since_hours: int | None = None, category: str | None = None, limit: int = 20, **_kwargs) -> dict:
    if conn is None:
        from custodian.db.connection import db_connection

        with db_connection() as local_conn:
            return check_system_updates(local_conn, since=since, since_hours=since_hours, category=category, limit=limit)
    cutoff = normalize_since_input(since, since_hours)
    query = (
        "SELECT id, title, description, category, project, created_at, created_by "
        "FROM system_updates WHERE created_at >= ?"
    )
    params: list[object] = [cutoff]
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 100)))
    rows = conn.execute(query, params).fetchall()
    updates = [dict(row) for row in rows]
    message = f"Found {len(updates)} update(s) since {cutoff}." if updates else f"No system updates since {cutoff}."
    return {"count": len(updates), "updates": updates, "message": message}


def get_update_list() -> str:
    if not os.path.isfile(ROADMAP_FILE_PATH):
        raise FileNotFoundError(f"roadmap file is missing at {ROADMAP_FILE_PATH}")
    with open(ROADMAP_FILE_PATH, encoding="utf-8") as handle:
        return handle.read()
