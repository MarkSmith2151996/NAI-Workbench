from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import lookup_symbol

METADATA = {'description': 'Find a function, class, component, or type by name using live tree-sitter parsing. Returns CURRENT file paths and line numbers (not from fossil — always accurate). Use this to find where something is defined.', 'input_schema': {'properties': {'exact': {'default': False, 'description': 'Exact name match only. Default: false.', 'type': 'boolean'}, 'project': {'description': 'Project name', 'type': 'string'}, 'symbol': {'description': 'Symbol name to search for (partial match supported)', 'type': 'string'}}, 'required': ['project', 'symbol'], 'type': 'object'}, 'name': 'lookup_symbol'}


async def handle(params: dict, db):
    result = lookup_symbol(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
