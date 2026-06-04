from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import run_in_box

METADATA = {'description': "Execute a command inside a project's persistent box. The box auto-provisions if needed.", 'input_schema': {'properties': {'command': {'description': 'Command to run inside the box.', 'type': 'string'}, 'project': {'description': 'Project name', 'type': 'string'}, 'timeout': {'default': 30, 'description': 'Timeout in seconds (default 30, max 120)', 'type': 'integer'}}, 'required': ['project', 'command'], 'type': 'object'}, 'name': 'run_in_box'}


async def handle(params: dict, db):
    result = run_in_box(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
