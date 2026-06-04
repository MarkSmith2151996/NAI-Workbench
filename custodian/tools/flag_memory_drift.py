from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import flag_memory_drift

METADATA = {'description': 'Flag a persistent memory as wrong or stale when memory drift affects planning or execution. Creates an MF-NNN memory drift flag for later librarian audit.', 'input_schema': {'properties': {'flagged_in_context': {'description': 'Chat, task, or session context that surfaced the memory drift.', 'type': 'string'}, 'memory_id': {'description': 'Memory ID that was found to be wrong or stale.', 'type': 'integer'}, 'reason': {'description': 'Concrete explanation of what was wrong and the impact it had.', 'type': 'string'}}, 'required': ['memory_id', 'reason', 'flagged_in_context'], 'type': 'object'}, 'name': 'flag_memory_drift'}


async def handle(params: dict, db):
    result = flag_memory_drift(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
