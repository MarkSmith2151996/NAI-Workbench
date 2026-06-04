from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import agent_update

METADATA = {'description': "Update an existing agent's configuration. Pass the agent name or ID and any fields to change.", 'input_schema': {'properties': {'agent': {'description': 'Agent name or ID to update', 'type': 'string'}, 'description': {'description': 'New description (optional)', 'type': 'string'}, 'max_turns': {'description': 'New max turns (optional)', 'type': 'integer'}, 'model': {'description': 'New OpenAI model ID (optional). Must match a currently-available model.', 'type': 'string'}, 'name': {'description': 'New name (optional)', 'type': 'string'}, 'project': {'description': 'New project binding (optional, empty string to unbind)', 'type': 'string'}, 'spec_path': {'description': "YAML spec path relative to the bound project's /workspace root", 'type': 'string'}, 'system_prompt': {'description': 'New system prompt (optional)', 'type': 'string'}, 'workstation': {'description': 'New workstation binding (optional, empty string to unbind)', 'type': 'string'}}, 'required': ['agent'], 'type': 'object'}, 'name': 'agent_update'}


async def handle(params: dict, db):
    result = agent_update(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
