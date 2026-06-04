from __future__ import annotations

import json

from mcp.types import TextContent

from custodian.services.workstations import get_instance_status, update_spec


METADATA = {
    "name": "workstation_update",
    "description": "Update a workstation spec. Warm instances are not reprovisioned automatically.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Existing workstation spec name."},
            "description": {"type": "string"},
            "services": {"type": "array", "items": {"type": "object"}},
            "deps": {"type": "array", "items": {}},
            "env_vars": {"type": "object"},
            "volumes": {"type": "array", "items": {}},
            "tool_definitions": {"type": "array", "items": {"type": "object"}},
            "image": {"type": "string"},
            "max_slots": {"type": "integer"},
            "browser_profile": {"type": "string"},
        },
        "required": ["name"],
    },
}


async def handle(params: dict, db):
    name = params["name"]
    updates = {key: value for key, value in params.items() if key != "name"}
    spec = update_spec(name, **updates)
    status = get_instance_status(name)
    note = None
    if status.get("instance") and status["instance"].get("status") == "warm":
        note = "A warm instance already exists; spec changes take effect on the next provision."
    return [TextContent(type="text", text=json.dumps({"spec": spec, "note": note}, indent=2))]
