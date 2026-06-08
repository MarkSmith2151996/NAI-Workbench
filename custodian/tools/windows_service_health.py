from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from mcp.types import TextContent

try:
    from _windows_ps import run_ps
except ModuleNotFoundError:  # pragma: no cover - depends on loader sys.path
    from custodian.tools._windows_ps import run_ps


TOOL_NAME = "windows_service_health"
TOOL_DESCRIPTION = "Check known Windows-adjacent service ports and HTTP health endpoints."
TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "services": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional subset of service names to check.",
        }
    },
}

METADATA = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMS}

KNOWN_SERVICES = {
    "keepa-downloader": {"port": 8095, "health_endpoint": "http://172.21.32.1:8095/health"},
    "chrome-cdp": {"port": 9222, "health_endpoint": "http://172.21.32.1:9222/json/version"},
    "steel": {
        "port": 3010,
        "health_endpoint": "http://127.0.0.1:3010/health",
        "fallback_health_endpoints": ["http://127.0.0.1:3010/v1/health"],
    },
}


def _json_response(payload: object):
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _http_health(url: str) -> tuple[bool, int | None, str | None]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            response.read(512)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            return 200 <= response.status < 400, elapsed_ms, None
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return False, elapsed_ms, f"HTTP {exc.code}"
    except Exception as exc:
        return False, None, str(exc)


def _service_http_health(config: dict) -> tuple[bool, int | None, str | None, str]:
    urls = [config["health_endpoint"], *config.get("fallback_health_endpoints", [])]
    last_error = None
    for url in urls:
        health_ok, response_ms, error = _http_health(url)
        if health_ok:
            return health_ok, response_ms, None, url
        last_error = error
    return False, None, last_error, urls[-1]


async def handle(params: dict, db):
    try:
        selected = params.get("services") or list(KNOWN_SERVICES)
        if not isinstance(selected, list):
            return _json_response({"error": "services must be a list of service names"})

        unknown = [name for name in selected if name not in KNOWN_SERVICES]
        if unknown:
            return _json_response({"error": f"Unknown services: {', '.join(unknown)}"})

        ports = [KNOWN_SERVICES[name]["port"] for name in selected]
        ports_literal = ", ".join(str(port) for port in ports)
        command = f"""
$ports = @({ports_literal})
$results = foreach ($port in $ports) {{
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  [ordered]@{{ port = [int]$port; port_open = [bool]$conn }}
}}
@($results) | ConvertTo-Json -Depth 3 -Compress
"""
        ps_result = run_ps(command, timeout=30)
        if ps_result["exit_code"] != 0:
            return _json_response({"error": ps_result["stderr"] or "PowerShell command failed"})

        raw_port_state = json.loads(str(ps_result["stdout"]))
        if isinstance(raw_port_state, dict):
            raw_port_state = [raw_port_state]
        port_state = {int(item["port"]): bool(item["port_open"]) for item in raw_port_state}

        with ThreadPoolExecutor(max_workers=len(selected) or 1) as executor:
            health_results = {
                name: executor.submit(_service_http_health, KNOWN_SERVICES[name])
                for name in selected
            }

        services = []
        for name in selected:
            config = KNOWN_SERVICES[name]
            health_ok, response_ms, error, checked_url = health_results[name].result()
            item = {
                "name": name,
                "port": config["port"],
                "port_open": port_state.get(config["port"], False),
                "health_ok": health_ok,
                "response_ms": response_ms,
                "health_endpoint": checked_url,
            }
            if error:
                item["error"] = error
            services.append(item)

        return _json_response({"services": services})
    except Exception as exc:
        return _json_response({"error": str(exc)})
