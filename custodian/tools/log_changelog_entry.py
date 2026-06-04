from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import log_changelog_entry

METADATA = {'description': 'Record a Custodian-meta changelog entry for a change shipped to the Custodian system or database. Can optionally link to friction points and append this CL ID to each referenced friction record.', 'input_schema': {'properties': {'related_task_id': {'description': 'Optional originating task ID such as CT-038.', 'type': 'string'}, 'resolves_friction': {'description': 'Optional array of FP IDs to link and append this CL ID to.', 'items': {'type': 'string'}, 'type': 'array'}, 'sub_items': {'description': 'Optional array of individual changes inside this logical unit.', 'items': {'type': 'string'}, 'type': 'array'}, 'summary': {'description': 'Paragraph-level description of the change.', 'type': 'string'}, 'title': {'description': 'Short one-line summary.', 'type': 'string'}}, 'required': ['title', 'summary'], 'type': 'object'}, 'name': 'log_changelog_entry'}


async def handle(params: dict, db):
    result = log_changelog_entry(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
