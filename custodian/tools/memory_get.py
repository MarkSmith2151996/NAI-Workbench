from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_get

METADATA = {'description': 'Retrieve a single memory by ID with full untruncated content. Use after memory_search or memory_context returns a truncated preview — pass the ID to get the complete body.', 'input_schema': {'properties': {'id': {'description': 'Memory ID to retrieve', 'type': 'integer'}}, 'required': ['id'], 'type': 'object'}, 'name': 'memory_get'}


async def handle(params: dict, db):
    result = memory_get(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
