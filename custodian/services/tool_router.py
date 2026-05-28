from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from custodian.db.connection import get_db
from custodian.db.native_extensions import get_extension, list_extensions
from custodian.services.box_bridge import call_project_tool
from custodian.services.native import call_extension


_mcp_tool_registry: dict[str, dict] = {}
_TOOL_DIR = Path(__file__).resolve().parent.parent / "tools"


def set_mcp_registry(registry: dict) -> None:
    global _mcp_tool_registry
    _mcp_tool_registry = dict(registry)


def _ensure_mcp_registry() -> None:
    global _mcp_tool_registry
    if _mcp_tool_registry:
        return

    registry: dict[str, dict] = {}
    importlib.invalidate_caches()
    for path in sorted(_TOOL_DIR.glob("*.py")):
        if path.name in {"__init__.py", "_template.py"} or path.name.startswith("_"):
            continue

        module_name = f"custodian.tools.{path.stem}"
        try:
            if module_name in sys.modules:
                del sys.modules[module_name]
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            metadata = getattr(module, "METADATA", None)
            handler = getattr(module, "handle", None)
            if isinstance(metadata, dict) and callable(handler) and "name" in metadata:
                registry[metadata["name"]] = {"metadata": metadata, "handler": handler}
        except Exception:
            continue

    _mcp_tool_registry = registry


def resolve_tool(tool_name: str, project: str | None = None):
    _ensure_mcp_registry()
    if tool_name in _mcp_tool_registry:
        return {
            "name": tool_name,
            "source": "mcp",
            "details": {"metadata": _mcp_tool_registry[tool_name]["metadata"]},
        }

    if project:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM tool_registry WHERE tool_name = ? AND project = ? ORDER BY updated_at DESC LIMIT 1",
                (tool_name, project),
            ).fetchone()
            if row is not None:
                tool = dict(row)
                return {"name": tool_name, "source": "box", "details": tool}
        finally:
            conn.close()

    conn = get_db()
    try:
        ext = get_extension(conn, tool_name)
        if ext:
            return {"name": tool_name, "source": "native_extension", "details": ext}
    finally:
        conn.close()

    return None


async def route_tool_call(tool_name: str, params: dict, project: str | None = None, db=None):
    resolution = resolve_tool(tool_name, project)
    if resolution is None:
        return {"error": f"Tool '{tool_name}' not found in any source (MCP, box, native extension)"}

    source = resolution["source"]
    if source == "mcp":
        handler = _mcp_tool_registry[tool_name]["handler"]
        if db is None:
            conn = get_db()
            try:
                return await handler(params, conn)
            finally:
                conn.close()
        return await handler(params, db)

    if source == "box":
        return call_project_tool(project=project, tool_name=tool_name, params=params)

    if source == "native_extension":
        endpoint = params.get("endpoint", "/")
        method = params.get("method", "POST")
        data = {k: v for k, v in params.items() if k not in ("endpoint", "method")}
        return call_extension(tool_name, endpoint=endpoint, method=method, data=data if data else None, timeout=params.get("timeout", 30))

    return {"error": f"Unknown source type: {source}"}


def list_all_tools(project: str | None = None):
    _ensure_mcp_registry()
    tools = []
    for name, entry in _mcp_tool_registry.items():
        tools.append({"name": name, "source": "mcp", "description": entry["metadata"].get("description", "")})

    if project:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT tool_name, description FROM tool_registry WHERE project = ? ORDER BY tool_name",
                (project,),
            ).fetchall()
            for row in rows:
                tools.append(
                    {
                        "name": row["tool_name"],
                        "source": "box",
                        "description": row["description"] or "",
                        "project": project,
                    }
                )
        finally:
            conn.close()

    conn = get_db()
    try:
        for ext in list_extensions(conn):
            tools.append({"name": ext["name"], "source": "native_extension", "description": ext.get("description", "")})
    finally:
        conn.close()
    return tools


def resolve_agent_tools(tool_names: list[str], project: str | None = None):
    resolved = []
    missing = []
    for name in tool_names:
        resolution = resolve_tool(name, project)
        if resolution:
            resolved.append(resolution)
        else:
            missing.append(name)
    return {"resolved": resolved, "missing": missing, "all_found": len(missing) == 0}
