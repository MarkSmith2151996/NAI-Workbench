from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent

from custodian.services.workstations import dispatch_agent


WORKSTATION_DISPATCH_TIMEOUT = 300


METADATA = {
    "name": "workstation_dispatch",
    "description": "Dispatch a workstation-bound agent to run a task inside its workstation slot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "Name of an active agent with a workstation binding."},
            "task": {"type": "string", "description": "Task to send to the agent loop."},
        },
        "required": ["agent_name", "task"],
    },
}


async def handle(params: dict, db):
    result = await asyncio.wait_for(
        asyncio.to_thread(dispatch_agent, params["agent_name"], params["task"]),
        timeout=WORKSTATION_DISPATCH_TIMEOUT,
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
