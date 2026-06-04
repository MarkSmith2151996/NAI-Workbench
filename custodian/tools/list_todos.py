from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.todo import list_todos

METADATA = {'description': 'List todo items by project scope and status.', 'input_schema': {'properties': {'include_all_statuses': {'default': False, 'description': 'If true, ignore the default open-only filter and show all statuses.', 'type': 'boolean'}, 'project': {'description': 'Optional project filter.', 'type': 'string'}, 'status': {'description': 'Optional status filter: open / done / promoted.', 'type': 'string'}, 'system_only': {'default': False, 'description': 'Only show system-wide todos (project IS NULL).', 'type': 'boolean'}}, 'type': 'object'}, 'name': 'list_todos'}


async def handle(params: dict, db):
    result = list_todos(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
