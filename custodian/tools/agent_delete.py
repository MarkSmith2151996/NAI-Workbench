from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import agent_delete

METADATA = {'description': "Delete an agent by name or ID (soft-delete — sets status to 'deleted').", 'input_schema': {'properties': {'agent': {'description': 'Agent name or ID to delete', 'type': 'string'}}, 'required': ['agent'], 'type': 'object'}, 'name': 'agent_delete'}


async def handle(params: dict, db):
    result = agent_delete(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
