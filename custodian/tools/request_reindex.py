from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.projects import request_reindex

METADATA = {'description': 'Request a fossil reindex for a project. Does NOT run immediately — creates a pending request the user must approve in the Admin TUI. Use when you notice a fossil is stale or missing information.', 'input_schema': {'properties': {'project': {'description': 'Project name', 'type': 'string'}, 'reason': {'description': 'Why reindexing is needed', 'type': 'string'}}, 'required': ['project', 'reason'], 'type': 'object'}, 'name': 'request_reindex'}


async def handle(params: dict, db):
    result = request_reindex(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
