from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import setup_project_folder

METADATA = {'description': 'Create a standardized shared folder for a project. Creates on WSL and optionally Mac. Registers paths in DB for tool discovery.', 'input_schema': {'properties': {'category': {'description': 'Folder purpose in lowercase kebab-case, e.g. outputs, wireframes, keepa-exports, reports.', 'type': 'string'}, 'include_mac': {'default': False, 'description': 'Also create the folder on the remote Mac via the laptop bridge. Default false.', 'type': 'boolean'}, 'project': {'description': 'Custodian project name, lowercase, matching projects.name.', 'type': 'string'}}, 'required': ['project', 'category'], 'type': 'object'}, 'name': 'setup_project_folder'}


async def handle(params: dict, db):
    result = setup_project_folder(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
