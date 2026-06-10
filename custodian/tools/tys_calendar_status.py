from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_calendar_status",
    "description": "Check ToldYouSo calendar stack readiness: is calendar_strategy.py running, is the DB populated for today, are reference prices fresh, and is the system ready to trade. Lists all issues if not ready.",
    "input_schema": {
        "properties": {},
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/calendar/status", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows?"}, indent=2))]
