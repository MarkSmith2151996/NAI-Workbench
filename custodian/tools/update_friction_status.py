from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import update_friction_status

METADATA = {'description': "Update a Custodian-meta friction point's status, and optionally overwrite its resolved_by or root_cause fields.", 'input_schema': {'properties': {'id': {'description': 'Friction point ID like FP-001.', 'type': 'string'}, 'resolved_by': {'description': 'Optional array of CL IDs to overwrite the existing resolved_by field.', 'items': {'type': 'string'}, 'type': 'array'}, 'root_cause': {'description': 'Optional root-cause text to overwrite the existing root_cause field.', 'type': 'string'}, 'status': {'description': 'New status: open, mitigated, resolved, or wontfix.', 'type': 'string'}}, 'required': ['id', 'status'], 'type': 'object'}, 'name': 'update_friction_status'}


async def handle(params: dict, db):
    result = update_friction_status(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
