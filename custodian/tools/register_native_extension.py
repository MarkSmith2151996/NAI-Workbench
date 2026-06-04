from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.native_extensions import register_extension
from custodian.services.native import check_health

METADATA = {
    "name": "register_native_extension",
    "description": "Register a new native extension service and immediately run a health check.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique extension name."},
            "host": {"type": "string", "description": "IP or hostname."},
            "port": {"type": "integer", "description": "Service port."},
            "description": {"type": "string", "description": "Optional description."},
            "base_path": {"type": "string", "description": "Optional URL base path.", "default": "/"},
            "protocol": {"type": "string", "description": "http or https.", "default": "http"},
            "health_endpoint": {"type": "string", "description": "Health endpoint path.", "default": "/health"},
            "project": {"type": "string", "description": "Optional project binding."},
        },
        "required": ["name", "host", "port"],
    },
}


async def handle(params: dict, db):
    result = register_extension(db, **params)
    if inspect.isawaitable(result):
        result = await result
    health = check_health(params["name"])
    return [TextContent(type="text", text=json.dumps({"extension": result, "health": health}, indent=2))]
