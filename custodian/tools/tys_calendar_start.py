from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_calendar_start",
    "description": "Start the ToldYouSo calendar stack: runs launch_calendar_stack.py which updates reference data, populates trades_today, and starts calendar_strategy.py. Returns pre/post state comparison. This is the fix button when the calendar stack isn't running.",
    "input_schema": {
        "properties": {},
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/calendar/start", method="POST", headers={"Content-Type": "application/json"}, data=b"{}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows? POST /calendar/start may take up to 30s."}, indent=2))]
