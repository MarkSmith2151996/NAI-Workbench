from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import get_recent_changes

METADATA = {'description': 'Get summarized recent commits for a project (from the latest fossil).', 'input_schema': {'properties': {'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'get_recent_changes'}


async def handle(params: dict, db):
    result = get_recent_changes(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
