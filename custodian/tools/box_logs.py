from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import box_logs

METADATA = {'description': "Read logs from a project's box.", 'input_schema': {'properties': {'filter': {'description': "Optional filter: 'error' or 'warning'.", 'type': 'string'}, 'lines': {'default': 50, 'description': 'Number of lines to read (default 50).', 'type': 'integer'}, 'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'box_logs'}


async def handle(params: dict, db):
    result = box_logs(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
