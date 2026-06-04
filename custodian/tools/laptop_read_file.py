from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import read_file

METADATA = {'description': 'Read a file from the REMOTE Mac (100.82.234.100) over Tailscale. NOT for local files — use the built-in Read tool for local/PC/WSL files. Paths must be macOS paths (e.g., /Users/<username>/file.txt). Windows paths like C:\\ or E:\\ are LOCAL — use Read tool instead.', 'input_schema': {'properties': {'limit': {'description': 'Max lines to return. Default: 2000.', 'type': 'integer'}, 'offset': {'description': 'Start line (1-based). Default: 1.', 'type': 'integer'}, 'path': {'description': "Absolute macOS path on the REMOTE Mac (e.g., '/Users/<username>/...')", 'type': 'string'}}, 'required': ['path'], 'type': 'object'}, 'name': 'laptop_read_file'}


async def handle(params: dict, db):
    result = read_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
