from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import get_project_folders

METADATA = {'description': 'List all registered shared folders for a project.', 'input_schema': {'properties': {'project': {'description': 'Custodian project name, lowercase, matching projects.name.', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'get_project_folders'}


async def handle(params: dict, db):
    result = get_project_folders(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
