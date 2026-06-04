from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BOX_BRIDGE_URL = os.environ.get("BOX_BRIDGE_URL", "http://127.0.0.1:9099")


def _json_request(method: str, path: str, payload: dict | None = None) -> object:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{BOX_BRIDGE_URL}{path}", data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"error": f"Bridge returned {exc.code}: {raw[:500]}"}
    except URLError as exc:
        return {"error": f"bridge request failed: {exc.reason}"}


def run_in_box(project: str, command: str, timeout: int = 30) -> object:
    return _json_request("POST", "/run", {"project": project, "command": command, "timeout": timeout})


def box_status(project: str | None = None) -> object:
    path = "/status"
    if project:
        path += f"?{urlencode({'project': project})}"
    return _json_request("GET", path)


def call_project_tool(project: str, tool_name: str, params: dict | None = None) -> object:
    return _json_request("POST", "/call-tool", {"project": project, "tool_name": tool_name, "params": params or {}})


def list_project_tools(project: str) -> object:
    return _json_request("GET", f"/tools/{project}")


def register_tool(name: str, project: str, handler_path: str, description: str = "", params_schema: dict | None = None, source_module: str | None = None, source_class: str | None = None, source_method: str | None = None, hook_point: str | None = None, return_type: str | None = None, known_side_effects: str | None = None, created_by: str = "box-bridge") -> object:
    return _json_request(
        "POST",
        "/register-tool",
        {
            "name": name,
            "project": project,
            "description": description,
            "params_schema": params_schema or {},
            "handler_path": handler_path,
            "source_module": source_module,
            "source_class": source_class,
            "source_method": source_method,
            "hook_point": hook_point,
            "return_type": return_type,
            "known_side_effects": known_side_effects,
            "created_by": created_by,
        },
    )
