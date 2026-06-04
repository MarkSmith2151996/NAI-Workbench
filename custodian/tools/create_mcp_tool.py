from __future__ import annotations

import json
import os
import re

from mcp.types import TextContent

METADATA = {
    "name": "create_mcp_tool",
    "description": "Create a new MCP tool by writing a Python file to the tools/ directory. Hot-reload picks it up automatically — no restart needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Tool name. Must be a valid lowercase Python identifier.",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what this tool does.",
            },
            "input_schema": {
                "type": "object",
                "description": "JSON Schema for the tool's input parameters.",
            },
            "handler_code": {
                "type": "string",
                "description": "Python code for the handle() function body. Has access to params and db.",
            },
            "imports": {
                "type": "string",
                "description": "Optional additional imports to add at the top of the file (one per line).",
            },
        },
        "required": ["name", "description", "input_schema", "handler_code"],
    },
}

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _indent(code: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in code.strip().splitlines())


async def handle(params: dict, db):
    name = params["name"]
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return [TextContent(type="text", text=json.dumps({"error": "Tool name must be a lowercase Python identifier (letters, numbers, underscores)"}, indent=2))]

    filepath = os.path.join(TOOLS_DIR, f"{name}.py")
    if os.path.exists(filepath):
        return [TextContent(type="text", text=json.dumps({"error": f"Tool '{name}' already exists. Use update_mcp_tool or delete it first."}, indent=2))]

    imports = (params.get("imports") or "").strip()
    import_lines = f"{imports}\n" if imports else ""
    file_content = f'''from __future__ import annotations\n\nimport json\nfrom mcp.types import TextContent\n{import_lines}\nMETADATA = {json.dumps({'name': name, 'description': params['description'], 'input_schema': params['input_schema']}, indent=4)}\n\n\nasync def handle(params: dict, db):\n{_indent(params['handler_code'], 4)}\n'''

    with open(filepath, "w", encoding="utf-8") as handle_file:
        handle_file.write(file_content)

    return [TextContent(type="text", text=json.dumps({"created": name, "file": filepath, "message": f"Tool '{name}' created. Hot-reload will pick it up in a few seconds."}, indent=2))]
