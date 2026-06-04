from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import test

METADATA = {'description': "Run the project's test suite and return results. Auto-detects test command (npm test, pytest) or accepts an override.", 'input_schema': {'properties': {'command': {'description': 'Override test command. Auto-detected if omitted.', 'type': 'string'}}, 'type': 'object'}, 'name': 'sandbox_test'}


async def handle(params: dict, db):
    result = test(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
