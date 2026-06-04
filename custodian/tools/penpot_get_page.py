from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.penpot import get_page

METADATA = {'description': 'Get the structure of a Penpot file page — component names, layout frames, text content. Use to understand a wireframe design.', 'input_schema': {'properties': {'file_id': {'description': 'Penpot file UUID (from penpot_list_projects).', 'type': 'string'}, 'page': {'description': 'Page name to get (optional — returns all pages if omitted).', 'type': 'string'}}, 'required': ['file_id'], 'type': 'object'}, 'name': 'penpot_get_page'}


async def handle(params: dict, db):
    result = get_page(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
