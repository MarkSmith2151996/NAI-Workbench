from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_store

METADATA = {'description': 'Save a memory with content, tags, optional project binding, and importance. Use for patterns, decisions, gotchas, preferences — anything worth remembering across sessions.', 'input_schema': {'properties': {'content': {'description': 'The memory content (plain text, can be multi-line)', 'type': 'string'}, 'importance': {'default': 5, 'description': '1-10 scale: 1-3 minor, 4-6 useful, 7-8 important, 9-10 critical. Default: 5.', 'type': 'integer'}, 'project': {'description': 'Project name to bind to (optional — omit for global memory)', 'type': 'string'}, 'tags': {'description': "Tags for categorization (e.g., ['gotcha', 'sqlite', 'networking'])", 'items': {'type': 'string'}, 'type': 'array'}}, 'required': ['content'], 'type': 'object'}, 'name': 'memory_store'}


async def handle(params: dict, db):
    result = memory_store(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
