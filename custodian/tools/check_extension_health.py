from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.native import check_all_health, check_health

METADATA = {
    "name": "check_extension_health",
    "description": "Check health for one native extension or all registered native extensions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional extension name. If omitted, check all."},
        },
    },
}


async def handle(params: dict, db):
    if params.get("name"):
        result = check_health(params["name"])
    else:
        result = check_all_health()
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
