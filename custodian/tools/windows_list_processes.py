from __future__ import annotations

import json
import re

from mcp.types import TextContent

try:
    from _windows_ps import run_ps
except ModuleNotFoundError:  # pragma: no cover - depends on loader sys.path
    from custodian.tools._windows_ps import run_ps


TOOL_NAME = "windows_list_processes"
TOOL_DESCRIPTION = "List Windows processes with optional regex filtering and memory/CPU/name sorting."
TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "filter": {"type": "string", "description": "Optional regex matched against process name."},
        "top_n": {"type": "integer", "default": 10, "description": "Maximum processes to return."},
        "sort_by": {
            "type": "string",
            "default": "memory",
            "enum": ["memory", "cpu", "name"],
            "description": "Sort by memory, cpu, or name.",
        },
    },
}

METADATA = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMS}


def _json_response(payload: object):
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _as_list(value: object) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


async def handle(params: dict, db):
    try:
        top_n = max(1, int(params.get("top_n", 10)))
        sort_by = str(params.get("sort_by", "memory"))
        if sort_by not in {"memory", "cpu", "name"}:
            return _json_response({"error": "sort_by must be one of: memory, cpu, name"})

        pattern = params.get("filter")
        if pattern:
            re.compile(str(pattern), re.IGNORECASE)

        where_clause = f"| Where-Object {{ $_.ProcessName -match {_ps_string(str(pattern))} }}" if pattern else ""
        sort_clause = {
            "name": "Sort-Object ProcessName",
            "cpu": "Sort-Object CPU -Descending",
            "memory": "Sort-Object WorkingSet64 -Descending",
        }[sort_by]

        command = f"""
$raw = @(Get-Process {where_clause})
$selected = @($raw | {sort_clause} | Select-Object -First {top_n} |
  Select-Object @{{Name='name';Expression={{$_.ProcessName}}}}, @{{Name='pid';Expression={{$_.Id}}}}, @{{Name='cpu_seconds';Expression={{if ($_.CPU) {{[math]::Round($_.CPU, 2)}} else {{0}}}}}}, @{{Name='memory_mb';Expression={{[math]::Round($_.WorkingSet64 / 1MB, 1)}}}})
[ordered]@{{ total_matched = $raw.Count; processes = $selected }} | ConvertTo-Json -Depth 4 -Compress
"""
        result = run_ps(command, timeout=30)
        if result["exit_code"] != 0:
            return _json_response({"error": result["stderr"] or "PowerShell command failed"})

        parsed = json.loads(str(result["stdout"]))
        processes = _as_list(parsed.get("processes") if isinstance(parsed, dict) else parsed)
        total_matched = int(parsed.get("total_matched", len(processes))) if isinstance(parsed, dict) else len(processes)
        if sort_by == "name":
            processes.sort(key=lambda process: str(process.get("name") or "").lower())
        elif sort_by == "cpu":
            processes.sort(key=lambda process: float(process.get("cpu_seconds") or 0), reverse=True)
        else:
            processes.sort(key=lambda process: float(process.get("memory_mb") or 0), reverse=True)

        return _json_response({"processes": processes[:top_n], "total_matched": total_matched})
    except Exception as exc:
        return _json_response({"error": str(exc)})
