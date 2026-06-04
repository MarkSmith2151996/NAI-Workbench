from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.pipelines import list_pipelines

METADATA = {'description': 'List registered pipelines with step counts and most recent run status.', 'input_schema': {'properties': {'status': {'description': 'Optional status filter.', 'type': 'string'}}, 'type': 'object'}, 'name': 'list_pipelines'}


async def handle(params: dict, db):
    result = list_pipelines(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
