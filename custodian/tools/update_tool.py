from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import update_tool

METADATA = {'description': "Update an existing project box tool's code, schemas, or description and reload the tool server.", 'input_schema': {'properties': {'description': {'description': 'Updated description.', 'type': 'string'}, 'handler_code': {'description': 'Updated Python source defining def handle(params).', 'type': 'string'}, 'input_schema': {'description': 'Updated input schema.', 'type': 'object'}, 'name': {'description': 'Tool name.', 'type': 'string'}, 'output_schema': {'description': 'Updated output schema.', 'type': 'object'}, 'project': {'description': 'Custodian project name.', 'type': 'string'}}, 'required': ['name', 'project'], 'type': 'object'}, 'name': 'update_tool'}


async def handle(params: dict, db):
    result = update_tool(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
