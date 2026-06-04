from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import exec_in_workstation


METADATA = {
    "name": "workstation_exec",
    "description": "Execute a command inside a workstation container, optionally within a slot working directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "spec_name": {"type": "string", "description": "Workstation spec name."},
            "command": {"type": "string", "description": "Shell command to run."},
            "slot_index": {"type": "integer", "description": "Optional slot index for cwd /workspace/slots/{slot_index}."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
        },
        "required": ["spec_name", "command"],
    },
}


async def handle(params: dict, db):
    result = exec_in_workstation(
        params["spec_name"],
        params["command"],
        slot_index=params.get("slot_index"),
        timeout=params.get("timeout", 30),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
