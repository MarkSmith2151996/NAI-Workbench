from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import get_tool_registry

METADATA = {'description': 'List all registered tools, optionally filtered by project.', 'input_schema': {'properties': {'project': {'description': 'Optional project name filter.', 'type': 'string'}}, 'type': 'object'}, 'name': 'get_tool_registry'}


async def handle(params: dict, db):
    result = get_tool_registry(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
