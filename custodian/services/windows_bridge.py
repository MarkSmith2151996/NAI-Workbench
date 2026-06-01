"""WSL-native Windows bridge - direct interop, no external server needed.

Replaces the old HTTP-based bridge (II-043/II-046/II-047). Uses WSL's
built-in filesystem mount (/mnt/c/, /mnt/e/, etc.) for file operations
and cmd.exe / powershell.exe for command execution.

Nothing to start, nothing to crash, nothing to keep alive.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path conversion
# ---------------------------------------------------------------------------

_WIN_HOME = os.environ.get("WINDOWS_HOME", r"C:\Users\Big A")


def _win_to_wsl(path: str) -> str:
    """Convert a Windows path (C:\\... or C:/...) to WSL /mnt/ form.
    Passes through paths that are already WSL-style."""
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return path


def _wsl_to_win(path: str) -> str:
    """Convert a WSL /mnt/ path back to Windows format for display."""
    if path.startswith("/mnt/") and len(path) >= 6:
        ch = path[5]
        if ch.isalpha():
            drive = ch.upper()
            rest = path[6:].lstrip("/")
            win = f"{drive}:\\{rest}".replace("/", "\\") if rest else f"{drive}:\\"
            return win
    return path


_WSL_HOME = _win_to_wsl(_WIN_HOME)


# ---------------------------------------------------------------------------
# Security: blocked paths
# ---------------------------------------------------------------------------

BLOCKED_PATHS = [
    "**/.ssh/id_*",
    "**/.ssh/*_key",
    "**/.gnupg/private-keys*",
    "**/*.pem",
    "**/AppData/Roaming/Microsoft/Credentials/**",
    "**/AppData/Local/Microsoft/Credentials/**",
]


def _is_blocked(wsl_path: str) -> bool:
    resolved = str(Path(wsl_path).resolve())
    for pattern in BLOCKED_PATHS:
        normed = pattern.replace("\\", "/")
        if normed.startswith("**/"):
            if fnmatch.fnmatch(resolved, normed) or fnmatch.fnmatch(
                os.path.basename(resolved), normed[3:]
            ):
                return True
        elif resolved == normed or resolved.startswith(normed.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool implementations - signatures match the old HTTP-client module exactly
# ---------------------------------------------------------------------------


def read_file(path: str, offset: int = 1, limit: int = 2000) -> object:
    wsl = _win_to_wsl(path)
    if _is_blocked(wsl):
        return {"error": f"Access denied: {path}"}
    if not os.path.isfile(wsl):
        return {"error": f"File not found: {path}"}
    try:
        with open(wsl, "r", errors="replace") as fh:
            lines = fh.readlines()
        start = max(0, offset - 1)
        selected = lines[start : start + limit]
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            text = line.rstrip("\n")
            if len(text) > 2000:
                text = text[:2000] + "... [truncated]"
            numbered.append(f"{i:6d}\t{text}")
        return "\n".join(numbered) or "(empty file)"
    except Exception as exc:
        return {"error": f"Error reading {path}: {exc}"}


def write_file(path: str, content: str) -> object:
    wsl = _win_to_wsl(path)
    if _is_blocked(wsl):
        return {"error": f"Access denied: {path}"}
    try:
        parent = os.path.dirname(wsl)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(wsl, "w") as fh:
            fh.write(content)
        size = os.path.getsize(wsl)
        return f"Wrote {size} bytes to {path}"
    except Exception as exc:
        return {"error": f"Error writing {path}: {exc}"}


def edit_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> object:
    wsl = _win_to_wsl(path)
    if _is_blocked(wsl):
        return {"error": f"Access denied: {path}"}
    if not os.path.isfile(wsl):
        return {"error": f"File not found: {path}"}
    try:
        with open(wsl, "r") as fh:
            content = fh.read()
        count = content.count(old_string)
        if count == 0:
            return {"error": f"old_string not found in {path}"}
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"old_string found {count} times in {path}. "
                    "Use replace_all=true or provide more context."
                )
            }
        new_content = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        with open(wsl, "w") as fh:
            fh.write(new_content)
        replacements = count if replace_all else 1
        return f"Replaced {replacements} occurrence(s) in {path}"
    except Exception as exc:
        return {"error": f"Error editing {path}: {exc}"}


def run_command(
    command: str, cwd: str | None = None, timeout: int = 120
) -> object:
    import base64

    win_cwd = cwd or _WIN_HOME
    wsl_cwd = _win_to_wsl(win_cwd)
    timeout = min(timeout, 600)

    dangerous = [
        "rm -rf /", "mkfs", "dd if=", "> /dev/sd",
        "format c:", "del /s /q c:\\", "rd /s /q c:\\",
        "remove-item -recurse -force c:\\",
        "remove-item c:\\ -recurse",
        "get-childitem c:\\ -recurse | remove-item",
    ]
    lowered = command.lower()
    for blocked in dangerous:
        if blocked in lowered:
            return {"error": f"Blocked potentially destructive command: {command}"}

    escaped_cwd = win_cwd.replace("'", "''")
    full_command = f"Set-Location -LiteralPath '{escaped_cwd}'; {command}"
    encoded = base64.b64encode(full_command.encode("utf-16-le")).decode("ascii")

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
            cwd=wsl_cwd if os.path.isdir(wsl_cwd) else _WSL_HOME,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "cwd": win_cwd,
            "exit_code": result.returncode,
            "stdout": (
                result.stdout[-10000:]
                if len(result.stdout) > 10000
                else result.stdout
            ),
            "stderr": (
                result.stderr[-5000:]
                if len(result.stderr) > 5000
                else result.stderr
            ),
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s: {command}"}
    except FileNotFoundError:
        return {
            "error": (
                "powershell.exe not found — WSL interop may be disabled. "
                "Check /etc/wsl.conf [interop] enabled=true and verify "
                "PATH includes /mnt/c/Windows/System32/WindowsPowerShell/v1.0"
            )
        }
    except Exception as exc:
        return {"error": f"Error running command: {exc}"}


def glob(pattern: str, path: str | None = None) -> object:
    win_base = path or _WIN_HOME
    wsl_base = _win_to_wsl(win_base)
    try:
        base_path = Path(wsl_base)
        raw = sorted(
            str(p) for p in base_path.glob(pattern) if not _is_blocked(str(p))
        )
        matches = [_wsl_to_win(m) for m in raw]
        total = len(matches)
        if total > 200:
            matches = matches[:200]
        return {
            "pattern": pattern,
            "base": win_base,
            "total": total,
            "showing": len(matches),
            "matches": matches,
        }
    except Exception as exc:
        return {"error": f"Error globbing: {exc}"}


def grep(
    pattern: str,
    path: str | None = None,
    glob_filter: str | None = None,
    context: int = 0,
    max_results: int = 50,
) -> object:
    win_path = path or _WIN_HOME
    wsl_path = _win_to_wsl(win_path)

    cmd = ["grep", "-rn", "--color=never"]
    if context > 0:
        cmd.append(f"-C{context}")
    if glob_filter:
        cmd += ["--include", glob_filter]
    cmd += ["-m", str(max_results), pattern, wsl_path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        raw_lines = (
            result.stdout.strip().split("\n") if result.stdout.strip() else []
        )
        lines = []
        for line in raw_lines[:max_results]:
            if line.startswith("/mnt/"):
                colon_idx = line.find(":", 6)
                if colon_idx > 0:
                    wsl_file = line[:colon_idx]
                    rest = line[colon_idx:]
                    line = _wsl_to_win(wsl_file) + rest
            lines.append(line)
        return {
            "pattern": pattern,
            "path": win_path,
            "tool": "grep (WSL native)",
            "match_count": len(lines),
            "output": "\n".join(lines),
        }
    except subprocess.TimeoutExpired:
        return {"error": "Grep timed out after 30s"}
    except Exception as exc:
        return {"error": f"Error searching: {exc}"}


def list_dir(path: str | None = None) -> object:
    win_path = path or _WIN_HOME
    wsl_path = _win_to_wsl(win_path)

    if not os.path.isdir(wsl_path):
        return {"error": f"Not a directory: {win_path}"}

    try:
        entries = []
        for name in sorted(os.listdir(wsl_path)):
            full = os.path.join(wsl_path, name)
            try:
                st = os.stat(full)
                entries.append(
                    {
                        "name": name,
                        "type": "dir" if os.path.isdir(full) else "file",
                        "size": st.st_size if os.path.isfile(full) else None,
                        "modified": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
                        ),
                    }
                )
            except OSError:
                entries.append({"name": name, "type": "unknown", "error": "stat failed"})
        return {"path": win_path, "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"error": f"Error listing {win_path}: {exc}"}


def system_info() -> object:
    info: dict[str, Any] = {}

    try:
        r = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                'ConvertTo-Json @{' 
                'hostname=$env:COMPUTERNAME;'
                'os=[System.Environment]::OSVersion.VersionString;'
                'user=$env:USERNAME;'
                'home=$env:USERPROFILE;'
                'arch=$env:PROCESSOR_ARCHITECTURE'
                '}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            info = json.loads(r.stdout.strip())
    except Exception:
        info = {"hostname": "unknown", "os": "Windows (query failed)"}

    try:
        usage = shutil.disk_usage("/mnt/c")
        info["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
        }
    except Exception:
        pass

    try:
        r = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "$os = Get-CimInstance Win32_OperatingSystem;"
                " ConvertTo-Json @{"
                "total_mb=[math]::Round($os.TotalVisibleMemorySize/1024);"
                "available_mb=[math]::Round($os.FreePhysicalMemory/1024);"
                "used_mb=[math]::Round(($os.TotalVisibleMemorySize"
                " - $os.FreePhysicalMemory)/1024)"
                "}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            info["memory"] = json.loads(r.stdout.strip())
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "tailscale status --json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            ts = json.loads(r.stdout)
            my_ip = (ts.get("TailscaleIPs") or [None])[0]
            info["tailscale"] = {
                "online": ts.get("BackendState") == "Running",
                "ip": my_ip,
                "hostname": ts.get("Self", {}).get("HostName", "unknown"),
            }
    except Exception:
        pass

    info["bridge_mode"] = "WSL native interop (no external server)"
    return info
