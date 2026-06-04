from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.windows_bridge import glob

METADATA = {'description': 'Find files matching a glob pattern on the Windows PC over the Windows bridge. NOT for local files — use the built-in Glob tool for local/PC/WSL files.', 'input_schema': {'properties': {'path': {'description': 'Base directory on the Windows PC (default: C:\\Users\\Big A)', 'type': 'string'}, 'pattern': {'description': "Glob pattern (e.g., '**/*.py')", 'type': 'string'}}, 'required': ['pattern'], 'type': 'object'}, 'name': 'windows_glob'}


async def handle(params: dict, db):
    result = glob(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
