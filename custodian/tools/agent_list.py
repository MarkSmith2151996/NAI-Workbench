from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import agent_list

METADATA = {'description': 'List all agents in the Agent Factory. Returns name, model, project, description, and recent run count for each agent.', 'input_schema': {'properties': {'status': {'default': 'active', 'description': "Filter by status: 'active' (default) or 'deleted'.", 'type': 'string'}}, 'type': 'object'}, 'name': 'agent_list'}


async def handle(params: dict, db):
    result = agent_list(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
