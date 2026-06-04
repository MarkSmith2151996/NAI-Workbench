from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import install

METADATA = {'description': 'Install dependencies for a sandbox project. NOTE: sandbox_start auto-installs from requirements.txt/package.json, so you only need this for extra packages not in the manifest. Accepts a list of packages (pip or npm), or auto-installs from requirements.txt / package.json. Uses pip3 for Python projects, npm for Node.js projects.', 'input_schema': {'properties': {'manager': {'description': 'Package manager to use. Auto-detected if omitted (pip for Python, npm for Node.js).', 'enum': ['pip', 'npm'], 'type': 'string'}, 'packages': {'description': "Specific packages to install (e.g., ['textual', 'rich']). If omitted, installs from requirements.txt or package.json.", 'items': {'type': 'string'}, 'type': 'array'}, 'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'sandbox_install'}


async def handle(params: dict, db):
    result = install(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
