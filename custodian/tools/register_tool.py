from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.tools_registry import register_tool

METADATA = {'description': "Register or update a tool's metadata in the tool registry. Records what module the tool wraps, the hook point, return type, and known side effects.", 'input_schema': {'properties': {'created_by': {'default': 'manual', 'description': 'Who registered the metadata. Defaults to manual.', 'type': 'string'}, 'hook_point': {'description': 'Where the wrapper intercepts the source behavior.', 'type': 'string'}, 'known_side_effects': {'description': 'Optional known side effects.', 'type': 'string'}, 'project': {'description': 'Custodian project name.', 'type': 'string'}, 'return_type': {'description': 'Wrapper return type, e.g. list[dict] of product records.', 'type': 'string'}, 'source_class': {'description': 'Optional source class, e.g. KeepaAnalyzer.', 'type': 'string'}, 'source_method': {'description': 'Optional source method, e.g. analyze.', 'type': 'string'}, 'source_module': {'description': 'Source module path, e.g. keepa_analyzer.py.', 'type': 'string'}, 'tool_name': {'description': 'MCP tool name.', 'type': 'string'}, 'wrapper_path': {'description': 'Wrapper path, e.g. tools/keepa_analyze.py.', 'type': 'string'}}, 'required': ['tool_name', 'project', 'source_module', 'hook_point', 'return_type', 'wrapper_path'], 'type': 'object'}, 'name': 'register_tool'}


async def handle(params: dict, db):
    result = register_tool(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
