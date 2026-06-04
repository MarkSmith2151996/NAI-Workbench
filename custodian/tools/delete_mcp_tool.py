from __future__ import annotations

import json
import os

from mcp.types import TextContent

METADATA = {
    "name": "delete_mcp_tool",
    "description": "Delete a user-created MCP tool file. Hot-reload removes it from the registry automatically.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool name to delete."},
        },
        "required": ["name"],
    },
}

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROTECTED_TOOLS = {
    "create_mcp_tool",
    "update_mcp_tool",
    "delete_mcp_tool",
    "register_native_extension",
    "list_native_extensions",
    "check_extension_health",
    "remove_native_extension",
    "call_native_extension",
}


async def handle(params: dict, db):
    name = params["name"]
    if name in PROTECTED_TOOLS:
        return [TextContent(type="text", text=json.dumps({"error": f"Refusing to delete protected tool '{name}'."}, indent=2))]

    filepath = os.path.join(TOOLS_DIR, f"{name}.py")
    if not os.path.exists(filepath):
        return [TextContent(type="text", text=json.dumps({"error": f"Tool '{name}' does not exist."}, indent=2))]

    os.remove(filepath)
    return [TextContent(type="text", text=json.dumps({"deleted": name, "file": filepath}, indent=2))]
