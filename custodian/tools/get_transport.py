from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.transports import get_transport


METADATA = {
    "name": "get_transport",
    "description": "Pull a context transport by CS-NNN ID. Returns the full context payload from another Claude session. Use when the user says 'pull up CS-001', 'get transport CS-001', 'load CS-001', or references a CS- ID. Sets pulled_at timestamp on first pull. Can be pulled multiple times.",
    "input_schema": {
        "type": "object",
        "required": ["cs_id"],
        "properties": {
            "cs_id": {
                "type": "string",
                "description": "Transport ID like CS-001",
            },
        },
    },
}


async def handle(params: dict, db):
    result = get_transport(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
