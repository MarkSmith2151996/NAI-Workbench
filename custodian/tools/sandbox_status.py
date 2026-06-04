from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import status

METADATA = {'description': 'Get the status of the sandbox process (running/stopped, PID, port, error count).', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'sandbox_status'}


async def handle(params: dict, db):
    result = status(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
