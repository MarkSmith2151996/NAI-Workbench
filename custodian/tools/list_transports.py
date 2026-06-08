from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.transports import list_transports


METADATA = {
    "name": "list_transports",
    "description": "List recent context transports. Filter by project or status (pending/pulled).",
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Max rows, default 10",
            },
            "project": {
                "type": "string",
                "description": "Filter by source or target project",
            },
            "status": {
                "type": "string",
                "description": "Filter: 'pending' (not yet pulled) or 'pulled'",
            },
        },
    },
}


async def handle(params: dict, db):
    result = list_transports(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
