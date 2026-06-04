from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.meta import log_friction_point

METADATA = {'description': 'Record a Custodian-meta friction point encountered while developing the system itself. Stores the surface event, project state context, chat session context, optional root cause, and optional changelog links.', 'input_schema': {'properties': {'chat_session_context': {'description': 'Claude session context and what we were trying to do.', 'type': 'string'}, 'project_state_context': {'description': 'Task, branch, and system-state context at the time.', 'type': 'string'}, 'resolved_by': {'description': "Optional array of CL IDs such as ['CL-003'].", 'items': {'type': 'string'}, 'type': 'array'}, 'root_cause': {'description': 'Optional current understanding of why it happened.', 'type': 'string'}, 'status': {'description': 'Optional status override. Default: open.', 'type': 'string'}, 'surface_event': {'description': 'Immediate observable event that triggered the log.', 'type': 'string'}, 'title': {'description': 'Short one-line summary.', 'type': 'string'}}, 'required': ['title', 'surface_event', 'project_state_context', 'chat_session_context'], 'type': 'object'}, 'name': 'log_friction_point'}


async def handle(params: dict, db):
    result = log_friction_point(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
