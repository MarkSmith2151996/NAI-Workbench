from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.shared import create_shared_folder

METADATA = {'description': "Create a per-project shared subfolder for OpenCode task outputs. Call this BEFORE submitting a task that will produce files Claude needs to read later. Folder structure is `/mnt/c/Users/Big A/custodian-shared/<project>/<category>/`. Category names must be lowercase kebab-case (e.g., 'verification-reports', 'audits', 'analyses'). Idempotent. After creation, reference the full path in your TASK body. Claude controls all folder creation; OpenCode does not call this tool. For persistent, discoverable folders, use setup_project_folder instead.", 'input_schema': {'properties': {'category': {'description': 'Shared subfolder name in lowercase kebab-case.', 'type': 'string'}, 'project': {'description': 'Custodian project name, lowercase, matching projects.name.', 'type': 'string'}}, 'required': ['project', 'category'], 'type': 'object'}, 'name': 'create_shared_folder'}


async def handle(params: dict, db):
    result = create_shared_folder(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
