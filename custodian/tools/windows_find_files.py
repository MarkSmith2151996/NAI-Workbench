from __future__ import annotations

import json

from mcp.types import TextContent

try:
    from _windows_ps import run_ps
except ModuleNotFoundError:  # pragma: no cover - depends on loader sys.path
    from custodian.tools._windows_ps import run_ps


TOOL_NAME = "windows_find_files"
TOOL_DESCRIPTION = "Find Windows files by directory, pattern, optional recent-days filter, and sort order."
TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Windows directory path to search."},
        "pattern": {"type": "string", "default": "*", "description": "File glob/filter pattern."},
        "recent_days": {"type": "integer", "description": "Only include files modified in the last N days."},
        "limit": {"type": "integer", "default": 20, "description": "Maximum files to return."},
        "sort_by": {
            "type": "string",
            "default": "modified",
            "enum": ["modified", "size", "name"],
            "description": "Sort by modified, size, or name.",
        },
    },
    "required": ["path"],
}

METADATA = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMS}


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _json_response(payload: object):
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


async def handle(params: dict, db):
    try:
        path = str(params["path"]).strip()
        if not path:
            return _json_response({"error": "path is required"})
        pattern = str(params.get("pattern") or "*")
        limit = max(1, int(params.get("limit", 20)))
        sort_by = str(params.get("sort_by", "modified"))
        if sort_by not in {"modified", "size", "name"}:
            return _json_response({"error": "sort_by must be one of: modified, size, name"})

        recent_days = params.get("recent_days")
        recent_filter = ""
        if recent_days is not None:
            recent_filter = f"$items = $items | Where-Object {{ $_.LastWriteTime -gt (Get-Date).AddDays(-{int(recent_days)}) }}"

        sort_expression = {
            "modified": "$items | Sort-Object LastWriteTime -Descending",
            "size": "$items | Sort-Object Length -Descending",
            "name": "$items | Sort-Object Name",
        }[sort_by]

        command = f"""
$path = {_ps_string(path)}
$pattern = {_ps_string(pattern)}
if (-not (Test-Path -LiteralPath $path)) {{
  [ordered]@{{ error = "Path not found: $path" }} | ConvertTo-Json -Compress
  exit 0
}}
$items = Get-ChildItem -LiteralPath $path -Filter $pattern -File -ErrorAction SilentlyContinue
{recent_filter}
$total = @($items).Count
$sorted = {sort_expression}
$files = @($sorted | Select-Object -First {limit} | ForEach-Object {{
  [ordered]@{{
    name = $_.Name
    path = $_.FullName
    size_bytes = $_.Length
    modified = $_.LastWriteTime.ToString('o')
  }}
}})
[ordered]@{{ files = $files; total_found = $total }} | ConvertTo-Json -Depth 4 -Compress
"""
        result = run_ps(command, timeout=45)
        if result["exit_code"] != 0:
            return _json_response({"error": result["stderr"] or "PowerShell command failed"})
        return _json_response(json.loads(str(result["stdout"])))
    except Exception as exc:
        return _json_response({"error": str(exc)})
