from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import get_instance_status, list_statuses


METADATA = {
    "name": "workstation_status",
    "description": "Get one workstation status or all active workstation statuses.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional workstation spec name."},
        },
    },
}


async def handle(params: dict, db):
    if params.get("name"):
        result = get_instance_status(params["name"])
    else:
        result = list_statuses()
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
