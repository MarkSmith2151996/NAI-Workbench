from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import get_agent_spec

METADATA = {'description': "Read an agent's full YAML spec from its project box. Returns the parsed spec including tools, schemas, model, and task template.", 'input_schema': {'properties': {'name': {'description': 'Agent name', 'type': 'string'}}, 'required': ['name'], 'type': 'object'}, 'name': 'get_agent_spec'}


async def handle(params: dict, db):
    result = get_agent_spec(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
