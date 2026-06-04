from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import edit_file

METADATA = {'description': 'Exact string replacement in a file on the REMOTE Mac over Tailscale. NOT for local files — use the built-in Edit tool for local/PC/WSL files.', 'input_schema': {'properties': {'new_string': {'description': 'Replacement text', 'type': 'string'}, 'old_string': {'description': 'Text to find and replace', 'type': 'string'}, 'path': {'description': "Absolute macOS path on the REMOTE Mac (e.g., '/Users/<username>/...')", 'type': 'string'}, 'replace_all': {'default': False, 'description': 'Replace all occurrences (default: false)', 'type': 'boolean'}}, 'required': ['path', 'old_string', 'new_string'], 'type': 'object'}, 'name': 'laptop_edit_file'}


async def handle(params: dict, db):
    result = edit_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
