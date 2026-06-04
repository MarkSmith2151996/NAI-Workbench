from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import allocate_slot


METADATA = {
    "name": "workstation_allocate",
    "description": "Allocate the first free isolated slot in a warm workstation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "spec_name": {"type": "string", "description": "Workstation spec name."},
            "agent_run_id": {"type": "integer", "description": "Optional agent_runs id to link."},
        },
        "required": ["spec_name"],
    },
}


async def handle(params: dict, db):
    result = allocate_slot(params["spec_name"], agent_run_id=params.get("agent_run_id"))
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
