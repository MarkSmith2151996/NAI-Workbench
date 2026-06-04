from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import agent_create

METADATA = {'description': "Create a new agent in the Agent Factory. Agents are stored in the shared Workbench database and can be run from any Claude session or the Admin TUI. At minimum provide a name and either system_prompt or spec_path. Use this when the user wants to create a persistent worker for a repeatable task. When called from a planning session, draft the system_prompt from project context rather than passing the user's words through verbatim. Pick an appropriate model — openai/gpt-5.4 is a good default, openai/gpt-5.4-mini-fast for cheap/fast tasks. Infer project from conversation context if possible.", 'input_schema': {'properties': {'description': {'description': 'Short human-readable description of what the agent does', 'type': 'string'}, 'max_turns': {'default': 20, 'description': 'Max agentic turns (default 20)', 'type': 'integer'}, 'model': {'default': 'openai/gpt-5.4', 'description': "OpenAI model ID. Must match one of the currently-available models from 'opencode models openai'.", 'type': 'string'}, 'name': {'description': "Unique agent name (e.g., 'code-reviewer', 'test-writer')", 'type': 'string'}, 'project': {'description': 'Project name to bind to (optional — sets working directory when running)', 'type': 'string'}, 'spec_path': {'description': "YAML spec path relative to the bound project's /workspace root", 'type': 'string'}, 'system_prompt': {'description': "The system prompt that defines the agent's behavior and expertise", 'type': 'string'}, 'workstation': {'description': 'Optional active workstation spec name to run this agent inside.', 'type': 'string'}}, 'required': ['name'], 'type': 'object'}, 'name': 'agent_create'}


async def handle(params: dict, db):
    result = agent_create(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
