from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_health",
    "description": "Check ToldYouSo trading system health: Redis, NinjaTrader, calendar stack, RSI 4060 stack, and heartbeats. Returns structured status for all components in one call.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/health", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows? Start with: cd E:\\ToldYouSo\\tools && E:\\Anacanda\\python.exe -m uvicorn tys_api:app --host 0.0.0.0 --port 8096"}, indent=2))]
