from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import list_projects

METADATA = {'description': 'List all registered projects with their status, stack, and last indexed time.', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'list_projects'}


async def handle(params: dict, db):
    result = list_projects(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
