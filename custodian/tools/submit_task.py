from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tasks import submit_task

METADATA = {'description': 'Submit a TASK.md to Custodian and receive a task ID. Use after designing a task with the user — the returned ID is how OpenCode picks up the task via get_task. Include the full markdown body verbatim. Returns an ID the planner reports back to the user so they can tell OpenCode which task to execute.', 'input_schema': {'properties': {'body': {'description': 'Full task markdown body.', 'type': 'string'}, 'created_by': {'default': 'claude', 'description': "Who created the task. Defaults to 'claude'.", 'type': 'string'}, 'project': {'description': 'Optional registered project name.', 'type': 'string'}, 'title': {'description': 'One-line summary of the task.', 'type': 'string'}}, 'required': ['title', 'body'], 'type': 'object'}, 'name': 'submit_task'}


async def handle(params: dict, db):
    result = submit_task(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
