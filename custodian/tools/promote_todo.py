from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.todo import promote_todo

METADATA = {'description': 'Promote a todo into a real Custodian task and link it back.', 'input_schema': {'properties': {'task_body': {'description': 'Full TASK.md body for the promoted task.', 'type': 'string'}, 'task_project': {'description': 'Optional project override for the submitted task.', 'type': 'string'}, 'todo_id': {'description': 'Todo ID like TD-005.', 'type': 'string'}}, 'required': ['todo_id', 'task_body'], 'type': 'object'}, 'name': 'promote_todo'}


async def handle(params: dict, db):
    result = promote_todo(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
