from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.system import add_system_update

METADATA = {'description': 'Record a system-wide update — new tools, rules, skills, or behavior changes — that running sessions should know about. Call this whenever you ship something that changes how agents work. Other sessions can then pick up the change via check_system_updates.', 'input_schema': {'properties': {'category': {'description': "Free-text category such as 'new-tool', 'new-rule', 'new-skill', 'behavior-change', or 'breaking-change'.", 'type': 'string'}, 'created_by': {'default': 'claude', 'description': "Who created the update. Defaults to 'claude'.", 'type': 'string'}, 'description': {'description': 'Full explanation.', 'type': 'string'}, 'project': {'description': 'Optional project name if update is project-specific.', 'type': 'string'}, 'title': {'description': 'One-line summary.', 'type': 'string'}}, 'required': ['title', 'description', 'category'], 'type': 'object'}, 'name': 'add_system_update'}


async def handle(params: dict, db):
    result = add_system_update(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
