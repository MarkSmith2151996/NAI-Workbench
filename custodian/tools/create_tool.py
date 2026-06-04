from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import create_tool

METADATA = {'description': 'Create a new project box tool, register its schemas, and reload the box tool server.', 'input_schema': {'properties': {'description': {'description': 'User-facing tool description.', 'type': 'string'}, 'handler_code': {'description': 'Full Python source defining def handle(params).', 'type': 'string'}, 'input_schema': {'description': 'JSON schema-like field map for tool input.', 'type': 'object'}, 'name': {'description': 'Tool name / Python filename stem.', 'type': 'string'}, 'output_schema': {'description': 'JSON schema-like field map for tool output.', 'type': 'object'}, 'project': {'description': 'Custodian project name.', 'type': 'string'}}, 'required': ['name', 'project', 'handler_code'], 'type': 'object'}, 'name': 'create_tool'}


async def handle(params: dict, db):
    result = create_tool(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
