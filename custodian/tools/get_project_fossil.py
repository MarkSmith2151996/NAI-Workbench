from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import get_project_fossil

METADATA = {'description': "Get the latest fossil (architecture summary, file tree, dependencies, known issues) for a project. This is the fastest way to understand a project's structure without exploring files.", 'input_schema': {'properties': {'include_file_tree': {'default': False, 'description': 'Include the full file tree (can be large). Default: false.', 'type': 'boolean'}, 'include_symbols': {'default': False, 'description': 'Include full symbol list from this fossil. Default: false.', 'type': 'boolean'}, 'project': {'description': "Project name (e.g., 'progress-tracker', 'finance95', 'bjtrader', 'fba-command-center')", 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'get_project_fossil'}


async def handle(params: dict, db):
    result = get_project_fossil(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
