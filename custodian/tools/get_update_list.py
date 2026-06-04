from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.system import get_update_list

METADATA = {'description': 'Read the Custodian roadmap markdown file Antonio uses for freeform system-addition notes. Returns the file contents as a string.', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'get_update_list'}


async def handle(params: dict, db):
    result = get_update_list(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
