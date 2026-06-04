from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import get_meta_summary

METADATA = {'description': 'Summarize Custodian-meta friction and changelog state: friction counts by status, recent changelog entries, and currently open friction points.', 'input_schema': {'properties': {}, 'type': 'object'}, 'name': 'get_meta_summary'}


async def handle(params: dict, db):
    result = get_meta_summary(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
