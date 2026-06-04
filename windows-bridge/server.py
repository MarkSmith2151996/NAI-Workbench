#!/usr/bin/env python3
"""NAI Workbench — Windows Bridge MCP Server.

Exposes file system and command execution tools on the Windows PC so that
Claude Code running in WSL can operate on Windows-native files and services.

Transport: HTTP + SSE (MCP Streamable HTTP transport)
Bind: Windows bridge target (0.0.0.0:8223) or BRIDGE_HOST override
Auth: Bearer token via BRIDGE_TOKEN env var
"""

from __future__ import annotations

import fnmatch
import hmac
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool


LISTEN_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8224"))
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")

_RATE_LIMIT = int(os.environ.get("BRIDGE_RATE_LIMIT", "10"))
_request_times: list[float] = []

BLOCKED_PATHS = [
    "**/.ssh/id_*",
    "**/.ssh/*_key",
    "**/.gnupg/private-keys*",
    "**/*.pem",
    "**/AppData/Roaming/Microsoft/Credentials/**",
    "**/AppData/Local/Microsoft/Credentials/**",
]
BLOCKED_PATTERNS = os.environ.get("BRIDGE_BLOCKED_PATHS", "").split(":") if os.environ.get("BRIDGE_BLOCKED_PATHS") else []
BLOCKED_PATHS.extend(BLOCKED_PATTERNS)


def _normalized_resolved_path(path: str) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _is_blocked(path: str) -> bool:
    """Check if a path matches the deny list."""
    resolved = _normalized_resolved_path(path)
    for pattern in BLOCKED_PATHS:
        normalized_pattern = pattern.replace("\\", "/")
        if normalized_pattern.startswith("**/"):
            if fnmatch.fnmatch(resolved, normalized_pattern) or fnmatch.fnmatch(os.path.basename(resolved), normalized_pattern[3:]):
                return True
        elif resolved == normalized_pattern or resolved.startswith(normalized_pattern.rstrip("/") + "/"):
            return True
    return False


def _check_rate_limit() -> bool:
    now = time.monotonic()
    while _request_times and _request_times[0] < now - 1.0:
        _request_times.pop(0)
    if len(_request_times) >= _RATE_LIMIT:
        return False
    _request_times.append(now)
    return True


app = Server("windows-bridge")


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="windows_read_file",
            description="Read a file from the Windows PC. Returns content with line numbers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path on the Windows PC"},
                    "offset": {"type": "integer", "description": "Start line (1-based). Default: 1."},
                    "limit": {"type": "integer", "description": "Max lines to return. Default: 2000."},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="windows_write_file",
            description="Write/overwrite a file on the Windows PC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="windows_edit_file",
            description="Exact string replacement in a file on the Windows PC (like Claude Code's Edit).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "old_string": {"type": "string", "description": "Text to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences (default: false)",
                        "default": False,
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
        Tool(
            name="windows_run_command",
            description="Execute a shell command on the Windows PC. Returns stdout, stderr, exit_code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Working directory (default: home)"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 120, max: 600)",
                        "default": 120,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="windows_glob",
            description="Find files matching a glob pattern on the Windows PC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py')"},
                    "path": {"type": "string", "description": "Base directory (default: home)"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="windows_grep",
            description="Search file contents with regex on the Windows PC (uses ripgrep if available, else findstr).",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (default: home)"},
                    "glob_filter": {"type": "string", "description": "File glob filter (e.g., '*.py')"},
                    "context": {"type": "integer", "description": "Lines of context around matches (default: 0)"},
                    "max_results": {"type": "integer", "description": "Max results (default: 50)"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="windows_list_dir",
            description="List directory contents with file types and sizes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: home)"},
                },
            },
        ),
        Tool(
            name="windows_system_info",
            description="Get Windows PC system info: OS, Python version, disk, memory, Tailscale status.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if not _check_rate_limit():
        return [TextContent(type="text", text="Rate limit exceeded. Try again in a moment.")]

    try:
        if name == "windows_read_file":
            return await handle_read_file(arguments)
        if name == "windows_write_file":
            return await handle_write_file(arguments)
        if name == "windows_edit_file":
            return await handle_edit_file(arguments)
        if name == "windows_run_command":
            return await handle_run_command(arguments)
        if name == "windows_glob":
            return await handle_glob(arguments)
        if name == "windows_grep":
            return await handle_grep(arguments)
        if name == "windows_list_dir":
            return await handle_list_dir(arguments)
        if name == "windows_system_info":
            return await handle_system_info(arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]


async def handle_read_file(args: dict[str, Any]):
    path = args["path"]
    offset = args.get("offset", 1)
    limit = args.get("limit", 2000)

    if _is_blocked(path):
        return [TextContent(type="text", text=f"Access denied: {path}")]
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"File not found: {path}")]

    try:
        with open(path, "r", errors="replace") as handle:
            lines = handle.readlines()
        start = max(0, offset - 1)
        end = start + limit
        selected = lines[start:end]
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            text = line.rstrip("\n")
            if len(text) > 2000:
                text = text[:2000] + "... [truncated]"
            numbered.append(f"{i:6d}\t{text}")
        result = "\n".join(numbered) or "(empty file)"
        return [TextContent(type="text", text=result)]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error reading {path}: {exc}")]


async def handle_write_file(args: dict[str, Any]):
    path = args["path"]
    content = args["content"]

    if _is_blocked(path):
        return [TextContent(type="text", text=f"Access denied: {path}")]

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        size = os.path.getsize(path)
        return [TextContent(type="text", text=f"Wrote {size} bytes to {path}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error writing {path}: {exc}")]


async def handle_edit_file(args: dict[str, Any]):
    path = args["path"]
    old_string = args["old_string"]
    new_string = args["new_string"]
    replace_all = args.get("replace_all", False)

    if _is_blocked(path):
        return [TextContent(type="text", text=f"Access denied: {path}")]
    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"File not found: {path}")]

    try:
        with open(path, "r") as handle:
            content = handle.read()
        count = content.count(old_string)
        if count == 0:
            return [TextContent(type="text", text=f"old_string not found in {path}")]
        if count > 1 and not replace_all:
            return [TextContent(
                type="text",
                text=(
                    f"old_string found {count} times in {path}. "
                    "Use replace_all=true to replace all, or provide more context to make it unique."
                ),
            )]
        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        with open(path, "w") as handle:
            handle.write(new_content)
        replacements = count if replace_all else 1
        return [TextContent(type="text", text=f"Replaced {replacements} occurrence(s) in {path}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error editing {path}: {exc}")]


async def handle_run_command(args: dict[str, Any]):
    command = args["command"]
    cwd = args.get("cwd", str(Path.home()))
    timeout = min(args.get("timeout", 120), 600)

    dangerous = [
        "rm -rf /",
        "mkfs",
        "dd if=",
        "> /dev/sd",
        "format c:",
        "del /s /q c:\\",
        "rd /s /q c:\\",
    ]
    lowered = command.lower()
    for blocked in dangerous:
        if blocked in lowered:
            return [TextContent(type="text", text=f"Blocked potentially destructive command: {command}")]

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = {
            "command": command,
            "cwd": cwd,
            "exit_code": result.returncode,
            "stdout": result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout,
            "stderr": result.stderr[-5000:] if len(result.stderr) > 5000 else result.stderr,
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]
    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text=f"Command timed out after {timeout}s: {command}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error running command: {exc}")]


async def handle_glob(args: dict[str, Any]):
    pattern = args["pattern"]
    base = args.get("path", str(Path.home()))

    try:
        base_path = Path(base)
        matches = sorted(str(p) for p in base_path.glob(pattern) if not _is_blocked(str(p)))
        total = len(matches)
        if total > 200:
            matches = matches[:200]
        result = {
            "pattern": pattern,
            "base": base,
            "total": total,
            "showing": len(matches),
            "matches": matches,
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error globbing: {exc}")]


async def handle_grep(args: dict[str, Any]):
    pattern = args["pattern"]
    path = args.get("path", str(Path.home()))
    glob_filter = args.get("glob_filter")
    context = args.get("context", 0)
    max_results = args.get("max_results", 50)

    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "--line-number", "--color=never", f"--max-count={max_results}"]
        if glob_filter:
            cmd += ["--glob", glob_filter]
        if context > 0:
            cmd += [f"-C{context}"]
        cmd += [pattern, path]
        tool_name = "ripgrep"
    else:
        if os.path.isfile(path):
            target = path
        else:
            target = os.path.join(path, glob_filter or "*")
        cmd = ["findstr", "/s", "/n", "/r", pattern, target]
        tool_name = "findstr"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        output = {
            "pattern": pattern,
            "path": path,
            "tool": tool_name,
            "match_count": len(lines),
            "output": "\n".join(lines[:max_results]),
        }
        return [TextContent(type="text", text=json.dumps(output, indent=2))]
    except subprocess.TimeoutExpired:
        return [TextContent(type="text", text="Grep timed out after 30s")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error searching: {exc}")]


async def handle_list_dir(args: dict[str, Any]):
    path = args.get("path", str(Path.home()))

    if not os.path.isdir(path):
        return [TextContent(type="text", text=f"Not a directory: {path}")]

    try:
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                stat = os.stat(full)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": stat.st_size if os.path.isfile(full) else None,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                })
            except OSError:
                entries.append({"name": name, "type": "unknown", "error": "stat failed"})

        result = {"path": path, "count": len(entries), "entries": entries}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error listing {path}: {exc}")]


def _parse_wmic_memory(output: str) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            continue
        try:
            values[key] = int(value)
        except ValueError:
            continue
    total_kb = values.get("TotalVisibleMemorySize")
    free_kb = values.get("FreePhysicalMemory")
    if total_kb is None or free_kb is None:
        return None
    total_mb = total_kb // 1024
    available_mb = free_kb // 1024
    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "used_mb": total_mb - available_mb,
    }


async def handle_system_info(args: dict[str, Any]):
    edition_fn = getattr(platform, "win32_edition", None)
    edition = edition_fn() if callable(edition_fn) else ""
    os_label = f"Windows {platform.version()}"
    if edition:
        os_label = f"{os_label} ({edition})"

    info: dict[str, Any] = {
        "hostname": platform.node(),
        "os": os_label,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "user": os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
        "home": str(Path.home()),
    }

    try:
        usage = shutil.disk_usage(str(Path.home()))
        info["disk"] = {
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
        }
    except Exception:
        pass

    try:
        import psutil  # type: ignore

        mem = psutil.virtual_memory()
        info["memory"] = {
            "total_mb": mem.total // (1024 ** 2),
            "available_mb": mem.available // (1024 ** 2),
            "used_mb": mem.used // (1024 ** 2),
        }
    except ImportError:
        try:
            result = subprocess.run(
                ["wmic", "OS", "get", "TotalVisibleMemorySize,FreePhysicalMemory", "/value"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            parsed = _parse_wmic_memory(result.stdout)
            if parsed:
                info["memory"] = parsed
        except Exception:
            pass
    except Exception:
        pass

    try:
        ts = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        if ts.returncode == 0:
            ts_data = json.loads(ts.stdout)
            self_node = ts_data.get("Self", {})
            info["tailscale"] = {
                "online": self_node.get("Online", False),
                "ip": self_node.get("TailscaleIPs", ["?"])[0] if self_node.get("TailscaleIPs") else "?",
                "hostname": self_node.get("HostName", "?"),
            }
    except Exception:
        info["tailscale"] = "not available"

    return [TextContent(type="text", text=json.dumps(info, indent=2))]


def create_starlette_app():
    """Build the Starlette ASGI application with SSE transport and auth middleware."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    sse_transport = SseServerTransport("/messages/")

    class TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {BRIDGE_TOKEN}"
            if hmac.compare_digest(auth, expected):
                return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    async def handle_sse(request: Request):
        async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())
        return Response()

    async def health(request: Request):
        return JSONResponse({"status": "ok", "server": "windows-bridge"})

    async def handle_tool(request: Request):
        try:
            body = await request.json()
            tool_name = body.get("tool", "")
            arguments = body.get("arguments", {})
            result = await call_tool(tool_name, arguments)
            texts = [content.text for content in result if hasattr(content, "text")]
            return JSONResponse({"result": texts[0] if len(texts) == 1 else texts})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def handle_download(request: Request):
        file_path = request.query_params.get("path", "")
        if not file_path:
            return Response("Missing 'path' query parameter", status_code=400)
        resolved = str(Path(file_path).resolve())
        if _is_blocked(resolved):
            return Response("Path is blocked", status_code=403)
        if not os.path.isfile(resolved):
            return Response(f"File not found: {resolved}", status_code=404)
        try:
            import mimetypes

            content_type = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
            with open(resolved, "rb") as handle:
                data = handle.read()
            return Response(
                content=data,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{os.path.basename(resolved)}"',
                    "Content-Length": str(len(data)),
                },
            )
        except Exception as exc:
            return Response(f"Error reading file: {exc}", status_code=500)

    return Starlette(
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/tool", endpoint=handle_tool, methods=["POST"]),
            Route("/download", endpoint=handle_download, methods=["GET"]),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
        middleware=[Middleware(TokenAuthMiddleware)],
    )


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn required. Install with: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    if not BRIDGE_TOKEN:
        print("[windows-bridge] ERROR: BRIDGE_TOKEN is required and the server will not start", file=sys.stderr)
        sys.exit(1)

    print(f"[windows-bridge] Starting on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
    print("[windows-bridge] Token auth enabled", file=sys.stderr)

    starlette_app = create_starlette_app()
    uvicorn.run(starlette_app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
