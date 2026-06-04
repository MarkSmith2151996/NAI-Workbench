from __future__ import annotations

import json
import urllib.error
import urllib.request

from custodian.db.connection import get_db
from custodian.db.native_extensions import get_extension, list_extensions, update_health_status


def call_extension(extension_name: str, endpoint: str, method: str = "GET", data: dict | None = None, timeout: int = 30):
    conn = get_db()
    try:
        ext = get_extension(conn, extension_name)
        if not ext:
            return {"error": f"Native extension '{extension_name}' not found"}

        url = f"{ext['protocol']}://{ext['host']}:{ext['port']}{ext['base_path'].rstrip('/')}{endpoint}"
        req_data = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=req_data, method=method.upper())
        if req_data is not None:
            req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body}
    except urllib.error.URLError as exc:
        return {"error": f"Failed to reach {extension_name}: {exc}"}
    finally:
        conn.close()


def check_health(extension_name: str):
    conn = get_db()
    try:
        ext = get_extension(conn, extension_name)
        if not ext:
            return {"name": extension_name, "status": "not_found"}

        url = f"{ext['protocol']}://{ext['host']}:{ext['port']}{ext['health_endpoint']}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                update_health_status(conn, extension_name, "active", None)
                return {"name": extension_name, "status": "active", "http_code": resp.status}
        except Exception as exc:
            update_health_status(conn, extension_name, "unreachable", str(exc))
            return {"name": extension_name, "status": "unreachable", "error": str(exc)}
    finally:
        conn.close()


def check_all_health():
    conn = get_db()
    try:
        extensions = list_extensions(conn)
    finally:
        conn.close()
    return [check_health(ext["name"]) for ext in extensions]
