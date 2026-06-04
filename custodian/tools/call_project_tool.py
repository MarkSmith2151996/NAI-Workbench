from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import call_project_tool

METADATA = {'description': "Execute a tool inside a project's box. Use GET /tools on the box to discover available tools first.", 'input_schema': {'properties': {'params': {'description': 'JSON params to pass to the tool handler', 'type': 'object'}, 'project': {'description': 'Project name', 'type': 'string'}, 'tool_name': {'description': 'Name of the tool to call', 'type': 'string'}}, 'required': ['project', 'tool_name'], 'type': 'object'}, 'name': 'call_project_tool'}


async def handle(params: dict, db):
    result = call_project_tool(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
