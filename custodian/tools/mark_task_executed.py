from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tasks import mark_task_executed

METADATA = {'description': 'Mark a task as executed and optionally record what changed. OpenCode calls this as its LAST step after completing a task. Pass files_modified, decisions, and unfinished to record the session update in the same call — no separate reporter agent needed. If files_modified is omitted, only the task status is updated (backward compatible).', 'input_schema': {'properties': {'ct_id': {'description': 'Task ID like CT-042 or AA-001.', 'type': 'string'}, 'decisions': {'description': "Non-obvious choices you made that weren't specified in the task. Empty array if none.", 'items': {'properties': {'decision': {'type': 'string'}, 'file': {'type': 'string'}, 'rationale': {'type': 'string'}}, 'type': 'object'}, 'type': 'array'}, 'files_modified': {'description': 'Exact relative paths of every file you modified.', 'items': {'type': 'string'}, 'type': 'array'}, 'notes': {'description': 'Optional execution notes for traceability.', 'type': 'string'}, 'produced_files': {'description': 'Optional output files to record with the task.', 'items': {'properties': {'description': {'type': 'string'}, 'path': {'type': 'string'}, 'size': {'type': 'integer'}}, 'required': ['path'], 'type': 'object'}, 'type': 'array'}, 'project': {'description': "Project name. If omitted, uses the task's project from the DB.", 'type': 'string'}, 'tokens_used': {'description': 'Optional token count for this task.', 'type': 'integer'}, 'unexecuted_steps': {'description': 'Any task steps not reflected in your changes. Empty array if none.', 'items': {'type': 'string'}, 'type': 'array'}, 'unfinished': {'description': "What's incomplete or worth watching. Empty string if nothing.", 'type': 'string'}}, 'required': ['ct_id'], 'type': 'object'}, 'name': 'mark_task_executed'}


async def handle(params: dict, db):
    result = mark_task_executed(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
