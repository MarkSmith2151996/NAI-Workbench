from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import list_memory_flags

METADATA = {'description': 'List memory drift flags for librarian audit. Returns open flags by default, including the current memory content joined from memories.', 'input_schema': {'properties': {'limit': {'default': 20, 'description': 'Max rows to return, default 20.', 'type': 'integer'}, 'memory_id': {'description': "Optional filter to a specific memory's drift flags.", 'type': 'integer'}, 'status': {'default': 'open', 'description': 'Optional status filter: open, resolved, or wontfix. Default: open.', 'type': 'string'}}, 'type': 'object'}, 'name': 'list_memory_flags'}


async def handle(params: dict, db):
    result = list_memory_flags(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
