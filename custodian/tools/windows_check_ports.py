from __future__ import annotations

import json

from mcp.types import TextContent

try:
    from _windows_ps import run_ps
except ModuleNotFoundError:  # pragma: no cover - depends on loader sys.path
    from custodian.tools._windows_ps import run_ps


TOOL_NAME = "windows_check_ports"
TOOL_DESCRIPTION = "Check whether Windows TCP ports are listening and identify owning processes."
TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "ports": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Windows local TCP ports to check.",
        }
    },
    "required": ["ports"],
}

METADATA = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMS}


def _json_response(payload: object):
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


async def handle(params: dict, db):
    try:
        raw_ports = params.get("ports")
        if not isinstance(raw_ports, list) or not raw_ports:
            return _json_response({"error": "ports must be a non-empty list of integers"})
        ports = [int(port) for port in raw_ports]
        ports_literal = ", ".join(str(port) for port in ports)

        command = f"""
$ports = @({ports_literal})
$results = foreach ($port in $ports) {{
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($conn) {{
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    [ordered]@{{
      port = [int]$port
      listening = $true
      address = $conn.LocalAddress
      pid = $conn.OwningProcess
      process = if ($proc) {{ $proc.ProcessName + '.exe' }} else {{ $null }}
    }}
  }} else {{
    [ordered]@{{ port = [int]$port; listening = $false }}
  }}
}}
[ordered]@{{ ports = @($results) }} | ConvertTo-Json -Depth 4 -Compress
"""
        result = run_ps(command, timeout=30)
        if result["exit_code"] != 0:
            return _json_response({"error": result["stderr"] or "PowerShell command failed"})
        return _json_response(json.loads(str(result["stdout"])))
    except Exception as exc:
        return _json_response({"error": str(exc)})
