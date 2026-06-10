from __future__ import annotations

import json
from mcp.types import TextContent
import urllib.request, json

METADATA = {
    "name": "tys_reference_prices",
    "description": "Check ToldYouSo reference price freshness: friday_rth_close, vix_5day_avg, prior_20d_avg_range_ticks. Flags stale data with days_old count. Monday Star cannot fire correctly with stale reference prices.",
    "input_schema": {
        "properties": {},
        "required": [],
        "type": "object"
    }
}


async def handle(params: dict, db):
    try:
        req = urllib.request.Request("http://172.21.32.1:8096/reference_prices", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [TextContent(type="text", text=json.dumps(data, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "hint": "Is tys_api.py running on Windows?"}, indent=2))]
