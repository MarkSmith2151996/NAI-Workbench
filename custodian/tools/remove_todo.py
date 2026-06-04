from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.todo import remove_todo

METADATA = {'description': 'Delete a todo item that is no longer relevant.', 'input_schema': {'properties': {'todo_id': {'description': 'Todo ID like TD-005.', 'type': 'string'}}, 'required': ['todo_id'], 'type': 'object'}, 'name': 'remove_todo'}


async def handle(params: dict, db):
    result = remove_todo(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
