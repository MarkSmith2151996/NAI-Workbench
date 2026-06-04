from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import get_changelog_entry

METADATA = {'description': 'Fetch the full stored record for a single Custodian-meta changelog entry by ID.', 'input_schema': {'properties': {'id': {'description': 'Changelog entry ID like CL-001.', 'type': 'string'}}, 'required': ['id'], 'type': 'object'}, 'name': 'get_changelog_entry'}


async def handle(params: dict, db):
    result = get_changelog_entry(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
