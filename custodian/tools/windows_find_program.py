from __future__ import annotations

import json

from mcp.types import TextContent

try:
    from _windows_ps import run_ps
except ModuleNotFoundError:  # pragma: no cover - depends on loader sys.path
    from custodian.tools._windows_ps import run_ps


TOOL_NAME = "windows_find_program"
TOOL_DESCRIPTION = "Find a Windows program by App Paths, Start Menu shortcuts, Program Files, and optional running process state."
TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Program name to find, e.g. NinjaTrader"},
        "check_running": {
            "type": "boolean",
            "default": True,
            "description": "Also check matching running processes.",
        },
    },
    "required": ["name"],
}

METADATA = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMS}


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _json_response(payload: object):
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


async def handle(params: dict, db):
    try:
        name = str(params["name"]).strip()
        if not name:
            return _json_response({"error": "name is required"})
        check_running = bool(params.get("check_running", True))
        check_running_ps = "$true" if check_running else "$false"

        command = rf"""
$ErrorActionPreference = 'SilentlyContinue'
$name = {_ps_string(name)}
$nameNoExt = [System.IO.Path]::GetFileNameWithoutExtension($name)
$searched = @('App Paths', 'Start Menu', 'Program Files')
$foundPath = $null
$source = $null

$appPathRoots = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths',
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths'
)
foreach ($root in $appPathRoots) {{
  if (-not $foundPath -and (Test-Path $root)) {{
    Get-ChildItem -Path $root | ForEach-Object {{
      if ($foundPath) {{ return }}
      $props = Get-ItemProperty -LiteralPath $_.PSPath
      $defaultPath = $props.'(default)'
      if ($_.PSChildName -like "*$name*" -or $_.PSChildName -like "*$nameNoExt*" -or $defaultPath -like "*$name*") {{
        $candidate = if ($defaultPath) {{ $defaultPath }} else {{ $props.Path }}
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {{
          $script:foundPath = $candidate
          $script:source = 'App Paths'
        }}
      }}
    }}
  }}
}}

if (-not $foundPath) {{
  $shortcutRoots = @(
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
  ) | Where-Object {{ $_ -and (Test-Path -LiteralPath $_) }}
  $shell = New-Object -ComObject WScript.Shell
  foreach ($root in $shortcutRoots) {{
    if ($foundPath) {{ break }}
    Get-ChildItem -LiteralPath $root -Filter *.lnk -Recurse | Where-Object {{ $_.BaseName -like "*$name*" -or $_.BaseName -like "*$nameNoExt*" }} | ForEach-Object {{
      if ($foundPath) {{ return }}
      $shortcut = $shell.CreateShortcut($_.FullName)
      if ($shortcut.TargetPath -and (Test-Path -LiteralPath $shortcut.TargetPath)) {{
        $script:foundPath = $shortcut.TargetPath
        $script:source = 'Start Menu'
      }}
    }}
  }}
}}

if (-not $foundPath) {{
  $programRoots = @($env:ProgramFiles, ${{env:ProgramFiles(x86)}}) | Where-Object {{ $_ -and (Test-Path -LiteralPath $_) }}
  foreach ($root in $programRoots) {{
    if ($foundPath) {{ break }}
    $match = Get-ChildItem -LiteralPath $root -Filter "*$name*.exe" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $match -and $nameNoExt -ne $name) {{
      $match = Get-ChildItem -LiteralPath $root -Filter "*$nameNoExt*.exe" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    }}
    if ($match) {{
      $foundPath = $match.FullName
      $source = 'Program Files'
    }}
  }}
}}

$proc = $null
if ({check_running_ps}) {{
  $proc = Get-Process | Where-Object {{ $_.Name -like "*$nameNoExt*" -or ($foundPath -and $_.Path -eq $foundPath) }} | Select-Object -First 1
}}

if ($foundPath) {{
  [ordered]@{{
    found = $true
    path = $foundPath
    source = $source
    running = [bool]$proc
    pid = if ($proc) {{ $proc.Id }} else {{ $null }}
    memory_mb = if ($proc) {{ [math]::Round($proc.WorkingSet64 / 1MB, 1) }} else {{ $null }}
  }} | ConvertTo-Json -Depth 4 -Compress
}} else {{
  [ordered]@{{ found = $false; searched = $searched }} | ConvertTo-Json -Depth 4 -Compress
}}
"""
        result = run_ps(command, timeout=45)
        if result["exit_code"] != 0:
            return _json_response({"error": result["stderr"] or "PowerShell command failed"})
        return _json_response(json.loads(str(result["stdout"])))
    except Exception as exc:
        return _json_response({"error": str(exc)})
