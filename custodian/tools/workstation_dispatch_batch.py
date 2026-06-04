from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent

from custodian.services.workstations import dispatch_batch


METADATA = {
    "name": "workstation_dispatch_batch",
    "description": "Dispatch multiple tasks to a workstation-bound agent with bounded parallelism.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "Name of an active agent with a workstation binding."},
            "tasks": {"type": "array", "description": "Task strings to dispatch in order.", "items": {"type": "string"}},
            "parallel": {"type": "integer", "description": "Maximum concurrent dispatches."},
        },
        "required": ["agent_name", "tasks"],
    },
}


async def handle(params: dict, db):
    result = await asyncio.to_thread(
        dispatch_batch,
        params["agent_name"],
        params.get("tasks") or [],
        params.get("parallel"),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
