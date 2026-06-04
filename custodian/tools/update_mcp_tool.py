from __future__ import annotations

import json
import os
import re

from mcp.types import TextContent

METADATA = {
    "name": "update_mcp_tool",
    "description": "Update an existing MCP tool file's metadata or handler body. Hot-reload picks up the change automatically.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Existing tool name."},
            "description": {"type": "string", "description": "Optional replacement description."},
            "input_schema": {"type": "object", "description": "Optional replacement input schema."},
            "handler_code": {"type": "string", "description": "Optional replacement handle() body."},
            "imports": {"type": "string", "description": "Optional replacement extra imports block."},
        },
        "required": ["name"],
    },
}

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _indent(code: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in code.strip().splitlines())


async def handle(params: dict, db):
    name = params["name"]
    filepath = os.path.join(TOOLS_DIR, f"{name}.py")
    if not os.path.exists(filepath):
        return [TextContent(type="text", text=json.dumps({"error": f"Tool '{name}' does not exist."}, indent=2))]

    with open(filepath, encoding="utf-8") as handle_file:
        content = handle_file.read()

    desc_match = re.search(r'"description":\s*(.+?),\n\s*"input_schema":', content, re.S)
    schema_match = re.search(r'"input_schema":\s*(\{.*?\})\n\}', content, re.S)
    imports_match = re.search(r"from mcp\.types import TextContent\n(.*?)\nMETADATA", content, re.S)
    handler_match = re.search(r"async def handle\(params: dict, db\):\n(.*)\Z", content, re.S)

    current_description = json.loads(desc_match.group(1).strip()) if desc_match else name
    current_schema = json.loads(schema_match.group(1)) if schema_match else {"type": "object", "properties": {}}
    current_imports = imports_match.group(1).rstrip() if imports_match else ""
    current_handler = handler_match.group(1).rstrip("\n") if handler_match else "    return [TextContent(type=\"text\", text=json.dumps({}))]"

    description = params.get("description", current_description)
    input_schema = params.get("input_schema", current_schema)
    imports = params.get("imports", current_imports)
    handler_code = params.get("handler_code", "\n".join(line[4:] if line.startswith("    ") else line for line in current_handler.splitlines()))

    import_lines = f"{imports.strip()}\n" if imports and imports.strip() else ""
    file_content = f'''from __future__ import annotations\n\nimport json\nfrom mcp.types import TextContent\n{import_lines}\nMETADATA = {json.dumps({'name': name, 'description': description, 'input_schema': input_schema}, indent=4)}\n\n\nasync def handle(params: dict, db):\n{_indent(handler_code, 4)}\n'''

    with open(filepath, "w", encoding="utf-8") as handle_file:
        handle_file.write(file_content)

    return [TextContent(type="text", text=json.dumps({"updated": name, "file": filepath}, indent=2))]
