from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_search

METADATA = {'description': 'Search memories using full-text search (FTS5 with bm25 ranking). Optionally filter by project and/or tags.', 'input_schema': {'properties': {'limit': {'default': 20, 'description': 'Max results (default 20)', 'type': 'integer'}, 'project': {'description': 'Filter by project name (optional)', 'type': 'string'}, 'query': {'description': 'Search query (FTS5 — supports AND, OR, NOT, phrases in quotes)', 'type': 'string'}, 'tags': {'description': 'Filter by tags — memory must have ALL specified tags', 'items': {'type': 'string'}, 'type': 'array'}}, 'type': 'object'}, 'name': 'memory_search'}


async def handle(params: dict, db):
    result = memory_search(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
