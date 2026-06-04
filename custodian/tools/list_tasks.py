from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tasks import list_tasks

METADATA = {'description': "List recent tasks. Filter by status ('open', 'executed', 'archived') or by project. Omit status to see recent tasks across all statuses.", 'input_schema': {'properties': {'limit': {'default': 20, 'description': 'Maximum number of rows to return. Default 20.', 'type': 'integer'}, 'project': {'description': 'Optional project filter.', 'type': 'string'}, 'status': {'description': 'Optional task status filter.', 'type': 'string'}}, 'type': 'object'}, 'name': 'list_tasks'}


async def handle(params: dict, db):
    result = list_tasks(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
