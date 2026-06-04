from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.tool_router import list_all_tools

METADATA = {
    "name": "list_all_tools",
    "description": "List every available tool across MCP, box, and native-extension sources.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Optional project context for including box tools."},
        },
    },
}


async def handle(params: dict, db):
    result = list_all_tools(params.get("project"))
    if inspect.isawaitable(result):
        result = await result
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
