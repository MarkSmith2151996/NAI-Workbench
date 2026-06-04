from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import get_project_state

METADATA = {'description': "Get a project's CURRENT state in one call: live git status + recent session updates + box health + file tree. Use at the start of a session alongside STATUS.md for full project orientation. Fast (<3s). Does not trigger reindexing.", 'input_schema': {'properties': {'include_file_tree': {'default': True, 'description': 'Include live file tree (capped at 500 entries). Default: true.', 'type': 'boolean'}, 'max_recent_commits': {'default': 10, 'description': 'How many recent commits to include. Default: 10.', 'type': 'integer'}, 'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'get_project_state'}


async def handle(params: dict, db):
    result = get_project_state(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
