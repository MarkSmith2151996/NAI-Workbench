#!/usr/bin/env python3
"""Standalone diagnostic MCP sidecar for Custodian."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.routing import Route


LISTEN_HOST = os.environ.get("SIDECAR_BIND", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SIDECAR_PORT", "8224"))
SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
SESSION_REGISTRY_PATH = os.environ.get(
    "SESSION_REGISTRY_PATH",
    str(Path(__file__).resolve().parent / "session_registry.db"),
)
MAIN_MCP_HOST = os.environ.get("MAIN_MCP_HOST", "127.0.0.1")
MAIN_MCP_PORT = int(os.environ.get("MAIN_MCP_PORT", "8223"))
MAIN_MCP_SERVICE = os.environ.get("MAIN_MCP_SERVICE", "custodian-mcp-http")
SIDECAR_SERVICE = os.environ.get("SIDECAR_SERVICE", "custodian-sidecar")
MAC_TAILSCALE_IP = os.environ.get("SIDECAR_MAC_IP", "100.82.234.100")
SIDECAR_PUBLIC_URL = os.environ.get("SIDECAR_PUBLIC_URL", "https://sidecar.lamannalogistics.com/")
MCP_SESSION_STALE_SECONDS = 300

CPU_HZ = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
LOG_PREFIX = "[custodian-sidecar]"
CONTAINER_PROJECT_RE = re.compile(r"^alpha-(?P<project>.+)$")
SS_PID_RE = re.compile(r"pid=(\d+)")
PING_LATENCY_RE = re.compile(r"time=([0-9.]+)\s*ms")

app = Server("custodian-sidecar")


def _log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_text(data: object):
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _is_authorized(headers) -> bool:
    if not SIDECAR_TOKEN:
        return True
    auth_header = headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False
    token = auth_header[7:]
    return hmac.compare_digest(token, SIDECAR_TOKEN)


class AuthenticatedStreamableHTTPApp:
    """Simple bearer-token guard around the MCP HTTP transport."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        if SIDECAR_TOKEN:
            headers = {
                key.decode("latin1").lower(): value.decode("latin1")
                for key, value in scope.get("headers", [])
            }
            if not _is_authorized(headers):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.session_manager.handle_request(scope, receive, send)


def _run_command(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _read_json_lines(command: list[str], timeout: int = 20) -> list[dict]:
    result = _run_command(command, timeout=timeout)
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _probe_http(url: str, timeout: int = 5) -> tuple[bool, int | None, float | None]:
    start = time.monotonic()
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            return True, response.getcode(), elapsed_ms
    except HTTPError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        return True, exc.code, elapsed_ms
    except (URLError, TimeoutError, ValueError, OSError):
        return False, None, None


def _listener_info(port: int) -> tuple[bool, int | None]:
    result = _run_command(["ss", "-tlnp"], timeout=5)
    if result.returncode != 0:
        return False, None

    for line in result.stdout.splitlines():
        if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
            continue
        pid_match = SS_PID_RE.search(line)
        return True, int(pid_match.group(1)) if pid_match else None
    return False, None


def _process_uptime_seconds(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            system_uptime = float(handle.read().split()[0])
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().split()
        start_ticks = int(fields[21])
        uptime = int(system_uptime - (start_ticks / CPU_HZ))
        return max(uptime, 0)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def _journal_lines(service: str, lines: int, filter_text: str | None = None) -> list[str]:
    result = _run_command(
        ["journalctl", "--user", "-u", service, "-n", str(lines), "--no-pager"],
        timeout=15,
    )
    if result.returncode != 0:
        return [line for line in result.stderr.splitlines() if line.strip()] or [
            f"Unable to read journal for {service}."
        ]

    entries = result.stdout.splitlines()
    if filter_text:
        lowered = filter_text.lower()
        entries = [line for line in entries if lowered in line.lower()]
    return entries


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _tcp_connections_on_port(port: int) -> list[str]:
    result = _run_command(["ss", "-tnp"], timeout=5)
    if result.returncode != 0:
        return []
    matches = []
    port_fragment = f":{port}"
    for line in result.stdout.splitlines():
        if port_fragment in line:
            matches.append(line)
    return matches


def _derive_project_name(container_name: str) -> str:
    match = CONTAINER_PROJECT_RE.match(container_name)
    if match:
        return match.group("project")
    if "_" in container_name:
        return container_name.split("_", 1)[0]
    return container_name


def _parse_memory_mb(raw_usage: str | None) -> float | None:
    if not raw_usage:
        return None
    used = raw_usage.split("/", 1)[0].strip()
    match = re.match(r"([0-9.]+)([KMG]i?B)", used)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    scale = {
        "KiB": 1 / 1024,
        "MiB": 1,
        "GiB": 1024,
        "KB": 1 / 1000,
        "MB": 1,
        "GB": 1000,
    }.get(unit)
    if scale is None:
        return None
    return round(value * scale, 2)


def _docker_containers() -> list[dict]:
    ps_rows = _read_json_lines(["docker", "ps", "-a", "--format", "json"])
    stats_rows = _read_json_lines(["docker", "stats", "--no-stream", "--format", "json"])
    stats_by_name = {row.get("Name"): row for row in stats_rows if row.get("Name")}
    containers = []
    for row in ps_rows:
        name = row.get("Names") or row.get("Name") or row.get("ID") or "unknown"
        stats = stats_by_name.get(name, {})
        containers.append(
            {
                "name": name,
                "project": _derive_project_name(name),
                "status": row.get("Status"),
                "uptime": row.get("RunningFor") or row.get("State"),
                "ports": row.get("Ports"),
                "memory_mb": _parse_memory_mb(stats.get("MemUsage")),
                "cpu_pct": stats.get("CPUPerc"),
                "container_id": row.get("ID"),
            }
        )
    return containers


def _find_container_for_project(project: str) -> dict | None:
    normalized = project.strip().lower()
    for container in _docker_containers():
        if container["project"].lower() == normalized:
            return container
        if container["name"].lower() == normalized:
            return container
        if container["name"].lower().startswith(f"alpha-{normalized}"):
            return container
    return None


@app.list_tools()
async def list_tools():
    return [
        Tool(name="mcp_health", description="Check if main MCP server process is alive.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="mcp_restart", description="Restart the main MCP systemd service.", inputSchema={"type": "object", "properties": {}}),
        Tool(
            name="mcp_logs",
            description="Read MCP server logs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {"type": "integer", "default": 50},
                    "filter": {"type": "string", "enum": ["error", "warning"]},
                },
            },
        ),
        Tool(name="mcp_sessions", description="List active MCP sessions from the session registry.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="box_health_all", description="Dashboard of all Docker containers.", inputSchema={"type": "object", "properties": {}}),
        Tool(
            name="box_restart",
            description="Restart a specific project container.",
            inputSchema={
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        ),
        Tool(name="ping_mac", description="Check Mac reachability over Tailscale.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="ping_tunnel", description="Check Cloudflare tunnel health.", inputSchema={"type": "object", "properties": {}}),
        Tool(
            name="service_check",
            description="Generic HTTP health probe.",
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "path": {"type": "string", "default": "/"},
                    "timeout": {"type": "integer", "default": 5},
                },
                "required": ["host", "port"],
            },
        ),
        Tool(name="system_vitals", description="WSL system health.", inputSchema={"type": "object", "properties": {}}),
        Tool(
            name="sidecar_logs",
            description="Read sidecar logs.",
            inputSchema={
                "type": "object",
                "properties": {"lines": {"type": "integer", "default": 50}},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "mcp_health":
            return _json_text(handle_mcp_health())
        if name == "mcp_restart":
            return _json_text(handle_mcp_restart())
        if name == "mcp_logs":
            return _json_text(handle_mcp_logs(arguments))
        if name == "mcp_sessions":
            return _json_text(handle_mcp_sessions())
        if name == "box_health_all":
            return _json_text(handle_box_health_all())
        if name == "box_restart":
            return _json_text(handle_box_restart(arguments))
        if name == "ping_mac":
            return _json_text(handle_ping_mac())
        if name == "ping_tunnel":
            return _json_text(handle_ping_tunnel())
        if name == "service_check":
            return _json_text(handle_service_check(arguments))
        if name == "system_vitals":
            return _json_text(handle_system_vitals())
        if name == "sidecar_logs":
            return _json_text(handle_sidecar_logs(arguments))
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]


def handle_mcp_health() -> dict:
    alive, pid = _listener_info(MAIN_MCP_PORT)
    http_ok, http_status, _elapsed_ms = _probe_http(f"http://{MAIN_MCP_HOST}:{MAIN_MCP_PORT}/health", timeout=3)
    if not alive and http_ok:
        alive = True
    return {
        "alive": alive,
        "pid": pid,
        "port": MAIN_MCP_PORT,
        "uptime_seconds": _process_uptime_seconds(pid),
        "last_check": _now_iso(),
        "http_status": http_status,
    }


def handle_mcp_restart() -> dict:
    result = _run_command(["systemctl", "--user", "restart", MAIN_MCP_SERVICE], timeout=20)
    time.sleep(2)
    alive, pid = _listener_info(MAIN_MCP_PORT)
    stderr = result.stderr.strip()
    return {
        "success": result.returncode == 0 and alive,
        "new_pid": pid,
        "message": stderr or ("Restarted successfully" if alive else "Restart command ran but listener not detected"),
    }


def handle_mcp_logs(arguments: dict) -> dict:
    lines = int(arguments.get("lines", 50) or 50)
    filter_text = arguments.get("filter")
    return {"lines": _journal_lines(MAIN_MCP_SERVICE, lines, filter_text)}


def handle_mcp_sessions() -> dict:
    sessions = []
    tcp_connections = _tcp_connections_on_port(MAIN_MCP_PORT)
    now = datetime.now(timezone.utc)
    if not os.path.exists(SESSION_REGISTRY_PATH):
        return {"sessions": [], "message": f"Session registry not found at {SESSION_REGISTRY_PATH}", "tcp_connections": tcp_connections}

    conn = sqlite3.connect(SESSION_REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, transport, connected_at, last_activity_at, status FROM sessions ORDER BY connected_at DESC"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        last_activity = _parse_timestamp(row["last_activity_at"])
        stale = False
        if row["status"] == "active" and last_activity is not None:
            stale = (now - last_activity).total_seconds() > MCP_SESSION_STALE_SECONDS
            if row["transport"] == "http" and not tcp_connections and stale:
                stale = True
        sessions.append(
            {
                "session_id": row["session_id"],
                "transport": row["transport"],
                "connected_at": row["connected_at"],
                "last_activity_at": row["last_activity_at"],
                "status": row["status"],
                "stale": stale,
            }
        )
    return {"sessions": sessions}


def handle_box_health_all() -> dict:
    containers = _docker_containers()
    cleaned = [
        {
            "name": item["name"],
            "project": item["project"],
            "status": item["status"],
            "uptime": item["uptime"],
            "ports": item["ports"],
            "memory_mb": item["memory_mb"],
            "cpu_pct": item["cpu_pct"],
        }
        for item in containers
    ]
    return {"containers": cleaned}


def handle_box_restart(arguments: dict) -> dict:
    project = str(arguments.get("project") or "").strip()
    if not project:
        return {"success": False, "container_id": None, "message": "project is required"}
    container = _find_container_for_project(project)
    if not container:
        return {"success": False, "container_id": None, "message": f"No container found for project '{project}'"}
    result = _run_command(["docker", "restart", container["container_id"]], timeout=30)
    return {
        "success": result.returncode == 0,
        "container_id": container["container_id"],
        "message": result.stdout.strip() or result.stderr.strip() or "restart attempted",
    }


def handle_ping_mac() -> dict:
    result = _run_command(["ping", "-c", "1", "-W", "3", MAC_TAILSCALE_IP], timeout=6)
    latency = None
    match = PING_LATENCY_RE.search(result.stdout)
    if match:
        latency = float(match.group(1))
    return {"reachable": result.returncode == 0, "latency_ms": latency}


def handle_ping_tunnel() -> dict:
    process_result = _run_command(["pgrep", "-x", "cloudflared"], timeout=5)
    pid = None
    if process_result.returncode == 0:
        first_line = process_result.stdout.strip().splitlines()[0]
        if first_line.isdigit():
            pid = int(first_line)
    tunnel_ok, tunnel_status, _ = _probe_http(SIDECAR_PUBLIC_URL, timeout=5)
    return {
        "process_alive": pid is not None,
        "pid": pid,
        "tunnel_responsive": tunnel_ok and tunnel_status is not None,
    }


def handle_service_check(arguments: dict) -> dict:
    host = str(arguments.get("host") or "").strip()
    port = int(arguments.get("port"))
    path = str(arguments.get("path") or "/")
    timeout = int(arguments.get("timeout", 5) or 5)
    if not path.startswith("/"):
        path = f"/{path}"
    reachable, status_code, response_time_ms = _probe_http(f"http://{host}:{port}{path}", timeout=timeout)
    return {
        "reachable": reachable,
        "http_status": status_code,
        "response_time_ms": response_time_ms,
    }


def handle_system_vitals() -> dict:
    meminfo = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])

    with open("/proc/loadavg", "r", encoding="utf-8") as handle:
        load_parts = handle.read().strip().split()

    total_mb = round(meminfo["MemTotal"] / 1024)
    available_mb = round(meminfo["MemAvailable"] / 1024)
    used_mb = total_mb - available_mb
    memory_pct = round((used_mb / total_mb) * 100, 2) if total_mb else 0.0

    df_result = _run_command(["df", "-B1", "/"], timeout=5)
    disk_used_gb = None
    disk_total_gb = None
    disk_pct = None
    if df_result.returncode == 0:
        lines = [line for line in df_result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                total_bytes = int(parts[1])
                used_bytes = int(parts[2])
                disk_total_gb = round(total_bytes / (1024 ** 3), 2)
                disk_used_gb = round(used_bytes / (1024 ** 3), 2)
                disk_pct = round((used_bytes / total_bytes) * 100, 2) if total_bytes else 0.0

    return {
        "cpu_load_1m": float(load_parts[0]),
        "cpu_load_5m": float(load_parts[1]),
        "memory_used_mb": used_mb,
        "memory_total_mb": total_mb,
        "memory_pct": memory_pct,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
    }


def handle_sidecar_logs(arguments: dict) -> dict:
    lines = int(arguments.get("lines", 50) or 50)
    return {"lines": _journal_lines(SIDECAR_SERVICE, lines)}


async def root(_request: StarletteRequest):
    tools = await list_tools()
    return JSONResponse(
        {
            "server": "custodian-sidecar",
            "transport": "streamable-http",
            "mcp_endpoint": "/mcp",
            "tool_count": len(tools),
            "auth_required": bool(SIDECAR_TOKEN),
        }
    )


async def health(request: StarletteRequest):
    if not _is_authorized(request.headers):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"status": "ok", "server": "custodian-sidecar", "transport": "streamable-http"})


async def handle_tool_request(request: StarletteRequest):
    if not _is_authorized(request.headers):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    tool_name = body.get("tool", "")
    arguments = body.get("arguments")
    if arguments is None:
        arguments = body.get("args", {})
    if not tool_name:
        return JSONResponse({"error": "missing tool"}, status_code=400)
    if not isinstance(arguments, dict):
        return JSONResponse({"error": "arguments must be an object"}, status_code=400)

    result = await call_tool(tool_name, arguments)
    texts = [content.text for content in result if hasattr(content, "text")]
    return JSONResponse({"result": texts[0] if len(texts) == 1 else texts})


def create_starlette_app() -> Starlette:
    session_manager = StreamableHTTPSessionManager(app=app, json_response=False, stateless=False)
    streamable_http_app = AuthenticatedStreamableHTTPApp(session_manager)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/", endpoint=root, methods=["GET"]),
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/tool", endpoint=handle_tool_request, methods=["POST"]),
            Mount("/mcp", app=streamable_http_app),
        ],
        lifespan=lifespan,
    )


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        _log("ERROR: uvicorn is required. Install with: pip install -r custodian/requirements.txt")
        return 1

    _log(f"Starting on {LISTEN_HOST}:{LISTEN_PORT}")
    _log(f"MCP endpoint: http://{LISTEN_HOST}:{LISTEN_PORT}/mcp")
    if SIDECAR_TOKEN:
        _log("Bearer token required on /health, /tool, and /mcp")
    else:
        _log("Running without bearer auth; rely on tunnel/network controls")
    uvicorn.run(create_starlette_app(), host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
