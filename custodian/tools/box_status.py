from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import box_status

METADATA = {'description': 'Check health of one or all project boxes.', 'input_schema': {'properties': {'project': {'description': 'Optional project name.', 'type': 'string'}}, 'type': 'object'}, 'name': 'box_status'}


async def handle(params: dict, db):
    result = box_status(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
