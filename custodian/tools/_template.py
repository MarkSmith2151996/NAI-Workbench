from __future__ import annotations

import json

from mcp.types import TextContent


METADATA = {
    "name": "tool_name",
    "description": "What this tool does.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


async def handle(params: dict, db):
    return [TextContent(type="text", text=json.dumps({"status": "ok"}, indent=2))]
