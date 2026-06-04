from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import resolve_memory_flag

METADATA = {'description': 'Resolve or wontfix an MF-NNN memory drift flag after librarian audit and record what was done.', 'input_schema': {'properties': {'id': {'description': 'Memory drift flag ID like MF-001.', 'type': 'string'}, 'resolved_by': {'description': 'What action was taken, or why the flag was cleared.', 'type': 'string'}, 'status': {'description': 'New status: resolved or wontfix.', 'type': 'string'}}, 'required': ['id', 'status', 'resolved_by'], 'type': 'object'}, 'name': 'resolve_memory_flag'}


async def handle(params: dict, db):
    result = resolve_memory_flag(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
