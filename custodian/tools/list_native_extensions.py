from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.native_extensions import list_extensions

METADATA = {
    "name": "list_native_extensions",
    "description": "List registered native extensions with optional project or status filters.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Optional project filter."},
            "status": {"type": "string", "description": "Optional status filter."},
        },
    },
}


async def handle(params: dict, db):
    result = list_extensions(db, **params)
    if inspect.isawaitable(result):
        result = await result
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
