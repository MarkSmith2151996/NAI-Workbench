from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import get_symbol_context

METADATA = {'description': "Get Sonnet's description and relationship analysis for a known symbol. Unlike lookup_symbol (which gives current location), this gives semantic understanding from the last fossil.", 'input_schema': {'properties': {'project': {'description': 'Project name', 'type': 'string'}, 'symbol': {'description': 'Symbol name (partial match supported)', 'type': 'string'}}, 'required': ['project', 'symbol'], 'type': 'object'}, 'name': 'get_symbol_context'}


async def handle(params: dict, db):
    result = get_symbol_context(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
