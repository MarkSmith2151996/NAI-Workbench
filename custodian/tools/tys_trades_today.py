from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_trades_today",
    "description": "Check ToldYouSo trades_today table: what's populated, whether it's stale, what strategies are expected today, and what's missing. Use this to verify the calendar dispatcher has run for today.",
    "input_schema": {
        "properties": {},
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/trades_today", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows?"}, indent=2))]
