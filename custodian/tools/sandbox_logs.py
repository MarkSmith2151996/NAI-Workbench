from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import logs

METADATA = {'description': 'Get recent sandbox output. Optionally filter to errors/warnings only.', 'input_schema': {'properties': {'filter': {'description': "Filter: 'error', 'warning', or omit for all output.", 'type': 'string'}, 'lines': {'default': 50, 'description': 'Number of lines to return (default 50).', 'type': 'integer'}}, 'type': 'object'}, 'name': 'sandbox_logs'}


async def handle(params: dict, db):
    result = logs(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
