from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import list_project_tools

METADATA = {'description': "List all tools available in a project's box.", 'input_schema': {'properties': {'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'list_project_tools'}


async def handle(params: dict, db):
    result = list_project_tools(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
