from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tasks import get_task

METADATA = {'description': "Retrieve a task's body by task ID. OpenCode calls this when the user says 'execute CT-NNN.' Returns the full TASK.md body, ready to be treated as the execution prompt. Returns warning metadata if the task was already executed.", 'input_schema': {'properties': {'ct_id': {'description': 'Task ID like CT-042 or AA-001.', 'type': 'string'}}, 'required': ['ct_id'], 'type': 'object'}, 'name': 'get_task'}


async def handle(params: dict, db):
    result = get_task(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
