from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.penpot import list_projects

METADATA = {'description': 'List all Penpot projects and their files (wireframes/designs).', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'penpot_list_projects'}


async def handle(params: dict, db):
    result = list_projects(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
