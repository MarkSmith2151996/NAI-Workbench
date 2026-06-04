from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.system import check_system_updates

METADATA = {'description': "Check for recent system-wide updates that running sessions should know about. Returns new tools, rules, skills, or behavior changes since the given time. Default window is the last 7 days. Call this when the user asks 'what's new', 'check for updates', 'sync with custodian', or similar.", 'input_schema': {'properties': {'category': {'description': 'Optional category filter.', 'type': 'string'}, 'limit': {'default': 20, 'description': 'Maximum number of rows to return. Default 20.', 'type': 'integer'}, 'since': {'description': 'Optional ISO timestamp to use as the lower time bound.', 'type': 'string'}, 'since_hours': {'default': 168, 'description': 'Optional rolling time window in hours. Defaults to 168 (7 days).', 'type': 'integer'}}, 'type': 'object'}, 'name': 'check_system_updates'}


async def handle(params: dict, db):
    result = check_system_updates(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
