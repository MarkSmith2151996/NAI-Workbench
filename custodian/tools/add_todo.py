from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.todo import add_todo

METADATA = {'description': 'Zero-friction capture for per-project or system-wide follow-up ideas.', 'input_schema': {'properties': {'description': {'description': 'Optional extra context.', 'type': 'string'}, 'priority': {'default': 'medium', 'description': 'Priority: low / medium / high. Default medium.', 'type': 'string'}, 'project': {'description': 'Optional project name. Null = system-wide todo.', 'type': 'string'}, 'title': {'description': 'The idea to capture.', 'type': 'string'}}, 'required': ['title'], 'type': 'object'}, 'name': 'add_todo'}


async def handle(params: dict, db):
    result = add_todo(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
