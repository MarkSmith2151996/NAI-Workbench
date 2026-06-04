from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import retire_workstation


METADATA = {
    "name": "workstation_retire",
    "description": "Retire a workstation: remove its container, stop its instance, and retire its spec.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Workstation spec name."},
        },
        "required": ["name"],
    },
}


async def handle(params: dict, db):
    result = retire_workstation(params["name"])
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
