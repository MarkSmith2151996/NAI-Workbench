from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.native_extensions import remove_extension

METADATA = {
    "name": "remove_native_extension",
    "description": "Remove a registered native extension.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Extension name."},
        },
        "required": ["name"],
    },
}


async def handle(params: dict, db):
    result = remove_extension(db, params["name"])
    if inspect.isawaitable(result):
        result = await result
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
