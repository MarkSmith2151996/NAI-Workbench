from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.todo import complete_todo

METADATA = {'description': 'Mark a todo item as done.', 'input_schema': {'properties': {'todo_id': {'description': 'Todo ID like TD-005.', 'type': 'string'}}, 'required': ['todo_id'], 'type': 'object'}, 'name': 'complete_todo'}


async def handle(params: dict, db):
    result = complete_todo(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
