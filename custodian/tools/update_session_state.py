from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tasks import update_session_state

METADATA = {'description': "Record a lightweight update describing what you just did in this project. Call this after any task that modifies files. This keeps the Custodian state current between deep fossil re-indexes so the next planning session sees your work. Use structured enumeration, not narrative — list what changed, what didn't, what you decided. Cheap: a single DB write, no LLM analysis.", 'input_schema': {'properties': {'decisions': {'description': "Non-obvious choices you made that weren't specified in the task. Empty array if none.", 'items': {'properties': {'decision': {'type': 'string'}, 'file': {'type': 'string'}, 'rationale': {'type': 'string'}}, 'type': 'object'}, 'type': 'array'}, 'files_modified': {'description': 'Exact relative paths of every file you modified.', 'items': {'type': 'string'}, 'type': 'array'}, 'project': {'description': "Project name (e.g., 'fba-command-center').", 'type': 'string'}, 'task': {'description': 'What you were trying to do. Paste the TASK.md title or a one-sentence description.', 'type': 'string'}, 'tokens_used': {'description': 'Optional. Token count for this task if available.', 'type': 'integer'}, 'unexecuted_steps': {'description': "Any numbered steps from the task that aren't reflected in your changes. Empty array if none.", 'items': {'type': 'string'}, 'type': 'array'}, 'unfinished': {'description': "One or two sentences on what's incomplete or worth watching. Empty string if nothing.", 'type': 'string'}}, 'required': ['project', 'task', 'files_modified'], 'type': 'object'}, 'name': 'update_session_state'}


async def handle(params: dict, db):
    result = update_session_state(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
