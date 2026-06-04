from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import restart

METADATA = {'description': 'Restart the sandbox process (stop + start with same command).', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'sandbox_restart'}


async def handle(params: dict, db):
    result = restart(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
