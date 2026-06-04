from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import install_deps

METADATA = {'description': "Install packages into a project's box.", 'input_schema': {'properties': {'manager': {'description': 'Optional package manager override.', 'enum': ['pip', 'npm'], 'type': 'string'}, 'packages': {'description': 'Optional package names to install.', 'items': {'type': 'string'}, 'type': 'array'}, 'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'install_deps'}


async def handle(params: dict, db):
    result = install_deps(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
