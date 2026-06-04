from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import grep

METADATA = {'description': 'Search file contents with regex on the REMOTE Mac over Tailscale. NOT for local files — use the built-in Grep tool for local/PC/WSL files.', 'input_schema': {'properties': {'context': {'description': 'Lines of context around matches (default: 0)', 'type': 'integer'}, 'glob_filter': {'description': "File glob filter (e.g., '*.py')", 'type': 'string'}, 'max_results': {'description': 'Max results (default: 50)', 'type': 'integer'}, 'path': {'description': 'Directory or file on the REMOTE Mac (default: /Users/<username>)', 'type': 'string'}, 'pattern': {'description': 'Regex pattern to search for', 'type': 'string'}}, 'required': ['pattern'], 'type': 'object'}, 'name': 'laptop_grep'}


async def handle(params: dict, db):
    result = grep(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
