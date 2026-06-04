from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.windows_bridge import read_file

METADATA = {'description': 'Read a file from the Windows PC over the Windows bridge. NOT for WSL/local repo files — use the built-in Read tool for local/PC/WSL files. Paths should be native Windows paths (e.g., C:\\Users\\Big A\\file.txt).', 'input_schema': {'properties': {'limit': {'description': 'Max lines to return. Default: 2000.', 'type': 'integer'}, 'offset': {'description': 'Start line (1-based). Default: 1.', 'type': 'integer'}, 'path': {'description': "Absolute Windows path on the Windows PC (e.g., 'C:\\Users\\Big A\\...')", 'type': 'string'}}, 'required': ['path'], 'type': 'object'}, 'name': 'windows_read_file'}


async def handle(params: dict, db):
    result = read_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
