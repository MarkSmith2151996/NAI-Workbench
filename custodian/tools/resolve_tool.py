from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.tool_router import resolve_tool

METADATA = {
    "name": "resolve_tool",
    "description": "Resolve where a tool lives across MCP tools, box tools, and native extensions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "Tool name to resolve."},
            "project": {"type": "string", "description": "Optional project context for box-tool resolution."},
        },
        "required": ["tool_name"],
    },
}


async def handle(params: dict, db):
    result = resolve_tool(params["tool_name"], params.get("project"))
    if inspect.isawaitable(result):
        result = await result
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
