from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_delete

METADATA = {'description': 'Permanently delete a memory by ID.', 'input_schema': {'properties': {'id': {'description': 'Memory ID to delete', 'type': 'integer'}}, 'required': ['id'], 'type': 'object'}, 'name': 'memory_delete'}


async def handle(params: dict, db):
    result = memory_delete(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
