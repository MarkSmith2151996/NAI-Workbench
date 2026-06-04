from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import trigger_custodian

METADATA = {'description': "Run Sonnet indexing for a specific project. Creates a new fossil. This is an async operation — results won't be immediate.", 'input_schema': {'properties': {'project': {'description': 'Project name to index', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'trigger_custodian'}


async def handle(params: dict, db):
    result = trigger_custodian(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
