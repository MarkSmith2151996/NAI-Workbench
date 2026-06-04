from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.tool_router import route_tool_call

METADATA = {
    "name": "route_tool_call",
    "description": "Call any tool by name through the unified router, regardless of whether it is an MCP tool, box tool, or native extension.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "Tool name to call."},
            "params": {"type": "object", "description": "Tool input params."},
            "project": {"type": "string", "description": "Optional project context for box-tool resolution."},
        },
        "required": ["tool_name"],
    },
}


async def handle(params: dict, db):
    result = route_tool_call(params["tool_name"], params.get("params", {}), project=params.get("project"), db=db)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, list):
        texts = [getattr(item, "text", None) for item in result if getattr(item, "text", None) is not None]
        normalized = texts[0] if len(texts) == 1 else texts
        if isinstance(normalized, str):
            try:
                normalized = json.loads(normalized)
            except json.JSONDecodeError:
                pass
        payload = {"result": normalized}
        return [TextContent(type="text", text=json.dumps(payload, indent=2))]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
