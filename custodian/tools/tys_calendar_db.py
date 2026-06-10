from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_calendar_db",
    "description": "Full ToldYouSo calendar database diagnostic: trades_today entries, today's and yesterday's events, all active strategies, total event count, max event date, and recent trade log. Use for deep debugging of the calendar pipeline.",
    "input_schema": {
        "properties": {},
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/calendar/db", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows?"}, indent=2))]
