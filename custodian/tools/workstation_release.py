from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import release_slot


METADATA = {
    "name": "workstation_release",
    "description": "Release a workstation slot while preserving its files for review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "slot_id": {"type": "integer", "description": "workstation_slots id."},
        },
        "required": ["slot_id"],
    },
}


async def handle(params: dict, db):
    result = release_slot(params["slot_id"])
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
