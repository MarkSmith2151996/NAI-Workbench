from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.native import call_extension

METADATA = {
    "name": "call_native_extension",
    "description": "Call any registered native extension endpoint by name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Registered extension name."},
            "endpoint": {"type": "string", "description": "API endpoint path."},
            "method": {"type": "string", "description": "HTTP method.", "default": "GET"},
            "data": {"type": "object", "description": "Optional JSON request body."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
        },
        "required": ["name", "endpoint"],
    },
}


async def handle(params: dict, db):
    result = call_extension(
        params["name"],
        params["endpoint"],
        method=params.get("method", "GET"),
        data=params.get("data"),
        timeout=params.get("timeout", 30),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
