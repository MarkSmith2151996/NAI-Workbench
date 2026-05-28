from __future__ import annotations

import json
import string
from typing import Any

from pydantic import BaseModel

from custodian.agents.schema import GenericStructuredResult, LlmAgentSpec, get_schema


def _format_task(task: str, input_payload: dict[str, Any]) -> str:
    formatter = string.Formatter()
    placeholders = {
        field_name
        for _, field_name, _, _ in formatter.parse(task)
        if field_name not in (None, "")
    }
    missing = sorted(field for field in placeholders if field not in input_payload)
    if missing:
        raise ValueError(f"Missing required input for prompt placeholder: {missing[0]}")
    return task.format(**input_payload)


def _format_input_schema(input_schema: dict[str, Any] | str | None) -> str:
    if input_schema is None:
        return "No parameters"
    if isinstance(input_schema, str):
        try:
            input_schema = json.loads(input_schema)
        except (json.JSONDecodeError, TypeError):
            return str(input_schema)
    if not isinstance(input_schema, dict):
        return str(input_schema)

    compact = {}
    for name, schema in input_schema.items():
        if not isinstance(schema, dict):
            compact[name] = "unknown"
            continue
        field_type = schema.get("type", "unknown")
        if field_type == "array" and isinstance(schema.get("items"), dict):
            field_type = f"array[{schema['items'].get('type', 'unknown')}]"
        compact[name] = field_type
    return json.dumps(compact, sort_keys=True)


def _describe_tools(tools: list[dict[str, Any]]) -> str:
    lines = []
    for tool in tools:
        lines.append(
            f"- {tool.get('name', '')}: {tool.get('description', '')}. Input: {_format_input_schema(tool.get('params', {}))}"
        )
    return "\n".join(lines)


def _describe_schema(schema: type[BaseModel]) -> str:
    if schema is GenericStructuredResult:
        return "GenericStructuredResult\n- Any JSON object is accepted."
    lines = [schema.__name__]
    for name, field in schema.model_fields.items():
        annotation = getattr(field.annotation, "__name__", repr(field.annotation))
        description = field.description or "No description."
        lines.append(f"- {name}: {annotation} - {description}")
    return "\n".join(lines)


def compile_prompt(spec: LlmAgentSpec, input_payload: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    output_schema = get_schema(spec.output)
    formatted_task = _format_task(spec.task, input_payload).strip()
    system_sections = [
        f"[Agent Task]\n{formatted_task}",
        f"[Available Tools]\n{_describe_tools(tools)}",
        """[Tool Calling Format]
When you need to use a tool, respond with ONLY this JSON (no other text):
{"tool_call": {"name": "<tool_name>", "params": {<params>}}}

You will receive the tool's result in the next message. Then continue or call another tool.

When you have the final answer, respond with ONLY this JSON (no other text):
{"final_answer": {<output matching the Output Schema>}}""",
        f"[Output Schema]\n{_describe_schema(output_schema)}",
    ]
    if spec.guidance:
        system_sections.append(f"[Guidance]\n{spec.guidance.strip()}")
    user = f"[Task]\n{formatted_task}\n\n[Input Payload]\n{json.dumps(input_payload, indent=2, sort_keys=True)}"
    return {"system": "\n\n".join(system_sections), "user": user, "model": spec.model, "output_schema": output_schema}
