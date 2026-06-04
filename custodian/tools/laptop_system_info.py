from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import system_info

METADATA = {'description': 'Get system info from the REMOTE Mac: OS, Python, disk, memory, Tailscale.', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'laptop_system_info'}


async def handle(params: dict, db):
    result = system_info(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
