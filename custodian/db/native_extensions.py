from __future__ import annotations

from datetime import datetime


def register_extension(
    conn,
    name: str,
    host: str,
    port: int,
    description: str | None = None,
    base_path: str = "/",
    protocol: str = "http",
    health_endpoint: str = "/health",
    project: str | None = None,
) -> dict:
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO native_extensions (
            name, description, host, port, base_path, protocol, health_endpoint,
            project, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(name) DO UPDATE SET
            description = excluded.description,
            host = excluded.host,
            port = excluded.port,
            base_path = excluded.base_path,
            protocol = excluded.protocol,
            health_endpoint = excluded.health_endpoint,
            project = excluded.project,
            status = 'active',
            updated_at = excluded.updated_at
        """,
        (name, description, host, int(port), base_path, protocol, health_endpoint, project, now),
    )
    row = conn.execute("SELECT * FROM native_extensions WHERE name = ?", (name,)).fetchone()
    conn.commit()
    return dict(row)


def list_extensions(conn, project: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM native_extensions WHERE 1=1"
    params: list[object] = []
    if project:
        query += " AND project = ?"
        params.append(project)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY name"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_extension(conn, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM native_extensions WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def update_extension(conn, name: str, **kwargs) -> dict | None:
    allowed = {
        "description",
        "host",
        "port",
        "base_path",
        "protocol",
        "health_endpoint",
        "project",
        "status",
        "last_health_check",
        "last_health_error",
    }
    updates = []
    params: list[object] = []
    for key, value in kwargs.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(value)
    if not updates:
        return get_extension(conn, name)
    updates.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(name)
    conn.execute(f"UPDATE native_extensions SET {', '.join(updates)} WHERE name = ?", params)
    conn.commit()
    return get_extension(conn, name)


def remove_extension(conn, name: str) -> dict:
    existing = get_extension(conn, name)
    if not existing:
        return {"error": f"Native extension '{name}' not found"}
    conn.execute("DELETE FROM native_extensions WHERE name = ?", (name,))
    conn.commit()
    return {"removed": name}


def update_health_status(conn, name: str, status: str, error: str | None = None) -> dict | None:
    now = datetime.now().isoformat()
    conn.execute(
        """
        UPDATE native_extensions
        SET status = ?,
            last_health_check = ?,
            last_health_error = ?,
            updated_at = ?
        WHERE name = ?
        """,
        (status, now, error, now, name),
    )
    conn.commit()
    return get_extension(conn, name)
