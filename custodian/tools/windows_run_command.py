from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.windows_bridge import run_command

METADATA = {'description': 'Execute a shell command on the Windows PC over the Windows bridge. NOT for local commands — use the built-in Bash tool for local/PC/WSL commands.', 'input_schema': {'properties': {'command': {'description': 'Shell command to run on the Windows PC', 'type': 'string'}, 'cwd': {'description': 'Working directory on the Windows PC (default: user home)', 'type': 'string'}, 'timeout': {'default': 120, 'description': 'Timeout in seconds (default: 120, max: 600)', 'type': 'integer'}}, 'required': ['command'], 'type': 'object'}, 'name': 'windows_run_command'}


async def handle(params: dict, db):
    result = run_command(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
