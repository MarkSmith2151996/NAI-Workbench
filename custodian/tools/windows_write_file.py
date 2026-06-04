from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.windows_bridge import write_file

METADATA = {'description': 'Write/overwrite a file on the Windows PC over the Windows bridge. NOT for local files — use the built-in Write tool for local/PC/WSL files.', 'input_schema': {'properties': {'content': {'description': 'File content to write', 'type': 'string'}, 'path': {'description': "Absolute Windows path on the Windows PC (e.g., 'C:\\Users\\Big A\\...')", 'type': 'string'}}, 'required': ['path', 'content'], 'type': 'object'}, 'name': 'windows_write_file'}


async def handle(params: dict, db):
    result = write_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
