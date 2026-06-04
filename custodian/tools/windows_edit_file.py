from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.windows_bridge import edit_file

METADATA = {'description': 'Exact string replacement in a file on the Windows PC over the Windows bridge. NOT for local files — use the built-in Edit tool for local/PC/WSL files.', 'input_schema': {'properties': {'new_string': {'description': 'Replacement text', 'type': 'string'}, 'old_string': {'description': 'Text to find and replace', 'type': 'string'}, 'path': {'description': "Absolute Windows path on the Windows PC (e.g., 'C:\\Users\\Big A\\...')", 'type': 'string'}, 'replace_all': {'default': False, 'description': 'Replace all occurrences (default: false)', 'type': 'boolean'}}, 'required': ['path', 'old_string', 'new_string'], 'type': 'object'}, 'name': 'windows_edit_file'}


async def handle(params: dict, db):
    result = edit_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
