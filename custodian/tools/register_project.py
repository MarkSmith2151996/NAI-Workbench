from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import register_project

METADATA = {'description': 'Register a new project in Custodian without provisioning or indexing it.', 'input_schema': {'properties': {'name': {'description': "Lowercase project key, e.g. 'finance95-web'. Must be unique.", 'type': 'string'}, 'path': {'description': 'Absolute filesystem path to the repo root.', 'type': 'string'}, 'stack': {'default': '', 'description': 'Tech stack description. Default empty string.', 'type': 'string'}, 'status': {'default': 'active', 'description': "Project status. Default 'active'.", 'type': 'string'}}, 'required': ['name', 'path'], 'type': 'object'}, 'name': 'register_project'}


async def handle(params: dict, db):
    result = register_project(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
