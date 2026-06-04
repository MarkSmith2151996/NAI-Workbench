from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.memory import memory_list

METADATA = {'description': 'Browse all memories sorted by importance (desc), then updated_at (desc). Supports pagination and project filter.', 'input_schema': {'properties': {'limit': {'default': 20, 'description': 'Max results (default 20)', 'type': 'integer'}, 'offset': {'default': 0, 'description': 'Skip first N results for pagination (default 0)', 'type': 'integer'}, 'project': {'description': 'Filter by project name (optional — omit for all memories)', 'type': 'string'}}, 'type': 'object'}, 'name': 'memory_list'}


async def handle(params: dict, db):
    result = memory_list(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
