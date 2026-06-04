from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import exec_command

METADATA = {'description': "Run a command inside a Docker sandbox container and return stdout/stderr. NOT for local commands — use the built-in Bash tool for local/PC/WSL commands. Use this to diagnose crashes, check files, or run one-off commands inside the project's Docker container (alpha-{project} containers). The sandbox process does NOT need to be running — only the Docker container.", 'input_schema': {'properties': {'command': {'description': 'Command to run (e.g., \'python3 -c "import fba_tui"\', \'cat /tmp/err.log\', \'pip list\')', 'type': 'string'}, 'project': {'description': 'Project name', 'type': 'string'}, 'timeout': {'description': 'Timeout in seconds (default 30, max 120)', 'type': 'integer'}}, 'required': ['project', 'command'], 'type': 'object'}, 'name': 'sandbox_exec'}


async def handle(params: dict, db):
    result = exec_command(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
