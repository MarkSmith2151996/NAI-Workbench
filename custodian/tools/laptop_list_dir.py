from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import list_dir

METADATA = {'description': "List directory contents on the REMOTE Mac over Tailscale. NOT for local dirs — use the built-in Bash 'ls' for local/PC/WSL directories.", 'input_schema': {'properties': {'path': {'description': 'Directory on the REMOTE Mac (default: /Users/<username>)', 'type': 'string'}}, 'type': 'object'}, 'name': 'laptop_list_dir'}


async def handle(params: dict, db):
    result = list_dir(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
