from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.penpot import export_svg

METADATA = {'description': 'Export a Penpot page or frame as SVG. Claude can read SVG as XML to understand layouts and visual structure.', 'input_schema': {'properties': {'file_id': {'description': 'Penpot file UUID.', 'type': 'string'}, 'page': {'description': 'Page name (optional — uses first page if omitted).', 'type': 'string'}}, 'required': ['file_id'], 'type': 'object'}, 'name': 'penpot_export_svg'}


async def handle(params: dict, db):
    result = export_svg(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
