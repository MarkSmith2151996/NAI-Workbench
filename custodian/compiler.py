from __future__ import annotations

import json
import sqlite3
from typing import Any


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name") or "")


def _tool_description(tool: dict[str, Any]) -> str:
    return str(tool.get("description") or "")


def _tool_params(tool: dict[str, Any]) -> dict[str, Any]:
    params = tool.get("input_schema") or tool.get("params")
    return params if isinstance(params, dict) else {}


def _format_input_schema(input_schema: dict[str, Any]) -> str:
    compact: dict[str, Any] = {}
    for name, schema in input_schema.items():
        if not isinstance(schema, dict):
            compact[name] = "unknown"
            continue
        field_type = schema.get("type", "unknown")
        if field_type == "array" and isinstance(schema.get("items"), dict):
            field_type = f"array[{schema['items'].get('type', 'unknown')}]"
        compact[name] = field_type
    return json.dumps(compact, sort_keys=True)


def _format_param_descriptions(input_schema: dict[str, Any]) -> str:
    parts: list[str] = []
    for name, schema in input_schema.items():
        if not isinstance(schema, dict):
            parts.append(f"{name} (unknown, optional)")
            continue
        field_type = schema.get("type", "unknown")
        required = "required" if schema.get("required") else "optional"
        description = str(schema.get("description") or "").strip()
        if description:
            parts.append(f"{name} ({field_type}, {required}): {description}")
        else:
            parts.append(f"{name} ({field_type}, {required})")
    return ", ".join(parts)


def _load_tool_details_from_db(tool: dict[str, Any], db: sqlite3.Connection | None) -> dict[str, Any]:
    if db is None:
        return tool
    name = _tool_name(tool)
    project = str(tool.get("project") or "")
    if not name or not project:
        return tool
    row = db.execute(
        "SELECT tool_name, project, description, input_schema, output_schema FROM tool_registry WHERE tool_name = ? AND project = ? AND status = 'active'",
        (name, project),
    ).fetchone()
    if not row:
        return tool
    merged = dict(tool)
    if row["description"]:
        merged["description"] = row["description"]
    if row["input_schema"]:
        try:
            merged["input_schema"] = json.loads(row["input_schema"])
        except json.JSONDecodeError:
            pass
    if row["output_schema"]:
        try:
            merged["output_schema"] = json.loads(row["output_schema"])
        except json.JSONDecodeError:
            pass
    return merged


def _describe_tools(tools: list[dict[str, Any]], db: sqlite3.Connection | None = None) -> str:
    if not tools:
        return "- No external tools available. Produce a final_answer directly."

    lines = []
    for tool in tools:
        full_tool = _load_tool_details_from_db(tool, db)
        params = _tool_params(full_tool)
        if params:
            param_desc = _format_param_descriptions(params)
            lines.append(f"- {_tool_name(full_tool)}: {_tool_description(full_tool)}. Parameters: {param_desc}")
        else:
            lines.append(f"- {_tool_name(full_tool)}: {_tool_description(full_tool)}")
    return "\n".join(lines)


def compile_prompt(
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    input_data: dict[str, Any],
    db: sqlite3.Connection | None = None,
) -> dict[str, str]:
    system_sections = [
        system_prompt.strip(),
        f"[Available Tools]\n{_describe_tools(tools, db)}",
        """[Tool Calling Format]
When you need to use a tool, respond with ONLY this JSON (no other text):
{"tool_call": {"name": "<tool_name>", "params": {<params>}}}

You will receive the tool's result in the next message. Then continue or call another tool.

When you have the final answer, respond with ONLY this JSON (no other text):
{"final_answer": {<final structured JSON>}}

Rules:
- One tool call per message
- Always wait for the result before calling the next tool
- If a tool returns an error, you may retry with different params or report the error in your final answer""",
    ]
    user = f"[Input Payload]\n{json.dumps(input_data, indent=2, sort_keys=True)}"
    return {
        "system": "\n\n".join(section for section in system_sections if section),
        "user": user,
    }
