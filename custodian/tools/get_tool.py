from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import get_tool

METADATA = {'description': "Retrieve a single tool's full registry record including schemas and status.", 'input_schema': {'properties': {'name': {'description': 'Tool name.', 'type': 'string'}, 'project': {'description': 'Custodian project name.', 'type': 'string'}}, 'required': ['name', 'project'], 'type': 'object'}, 'name': 'get_tool'}


async def handle(params: dict, db):
    result = get_tool(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
