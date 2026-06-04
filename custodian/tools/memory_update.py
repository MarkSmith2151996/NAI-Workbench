from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_update

METADATA = {'description': "Update a memory's content, tags, or importance by ID.", 'input_schema': {'properties': {'content': {'description': 'New content (optional)', 'type': 'string'}, 'id': {'description': 'Memory ID to update', 'type': 'integer'}, 'importance': {'description': 'New importance (1-10)', 'type': 'integer'}, 'tags': {'description': 'New tags (replaces existing)', 'items': {'type': 'string'}, 'type': 'array'}}, 'required': ['id'], 'type': 'object'}, 'name': 'memory_update'}


async def handle(params: dict, db):
    result = memory_update(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
