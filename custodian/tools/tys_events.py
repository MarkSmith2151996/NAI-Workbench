from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_events",
    "description": "Show upcoming ToldYouSo economic events with strategy mapping. Shows which events trigger which strategies and on what date. Default 7 days lookahead.",
    "input_schema": {
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days to look ahead (default 7)",
                "default": 7
            }
        },
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    days = params.get("days", 7)
    try:
        req = urllib.request.Request(f"http://172.21.32.1:8096/events?days={days}", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows?"}, indent=2))]
