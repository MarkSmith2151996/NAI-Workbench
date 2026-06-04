from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import find_related_files

METADATA = {'description': 'Given a symbol or concept, find all files that would likely need changes. Uses relationship data from fossils.', 'input_schema': {'properties': {'project': {'description': 'Project name', 'type': 'string'}, 'symbol': {'description': 'Symbol name or concept to find related files for', 'type': 'string'}}, 'required': ['project', 'symbol'], 'type': 'object'}, 'name': 'find_related_files'}


async def handle(params: dict, db):
    result = find_related_files(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
