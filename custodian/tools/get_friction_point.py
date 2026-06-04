from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import get_friction_point

METADATA = {'description': 'Fetch the full stored record for a single Custodian-meta friction point by ID.', 'input_schema': {'properties': {'id': {'description': 'Friction point ID like FP-001.', 'type': 'string'}}, 'required': ['id'], 'type': 'object'}, 'name': 'get_friction_point'}


async def handle(params: dict, db):
    result = get_friction_point(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
