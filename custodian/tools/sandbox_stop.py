from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import stop

METADATA = {'description': 'Stop the currently running sandbox process.', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'sandbox_stop'}


async def handle(params: dict, db):
    result = stop(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
