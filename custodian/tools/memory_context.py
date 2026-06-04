from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_context

METADATA = {'description': 'Load relevant memories for a session. Returns a 3-pass merge: high-importance (>=7), recently-accessed, and topic-matched (FTS). Call at session start to prime context.', 'input_schema': {'properties': {'limit': {'default': 30, 'description': 'Max total memories to return (default 30)', 'type': 'integer'}, 'project': {'description': 'Project name to load context for (optional — global only if omitted)', 'type': 'string'}, 'topics': {'description': 'Topic keywords to match against (optional)', 'items': {'type': 'string'}, 'type': 'array'}}, 'type': 'object'}, 'name': 'memory_context'}


async def handle(params: dict, db):
    result = memory_context(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
