from __future__ import annotations

import inspect
import json
import traceback

from mcp.types import TextContent
from custodian.agents.executor import execute_agent
from custodian.agents.spec_loader import load_spec
from custodian.db.agents import agent_run as db_agent_run, get_agent_spec

METADATA = {'description': "Run an agent via Claude CLI and return the result. The agent runs as a subprocess with its configured model, system prompt, and project context. Pass an optional 'prompt' to override the default starter prompt. Returns the agent's output text, token usage, and cost.", 'input_schema': {'properties': {'agent': {'description': 'Agent name or ID to run', 'type': 'string'}, 'input': {'description': 'Input payload for YAML-backed agents. Keys must match {placeholder} names in the YAML task template.', 'type': 'object'}, 'prompt': {'description': 'Task/prompt to send to the agent (overrides default)', 'type': 'string'}}, 'required': ['agent'], 'type': 'object'}, 'name': 'agent_run'}


async def handle(params: dict, db):
    try:
        spec_data = await get_agent_spec(db, name=params["agent"])
        if isinstance(spec_data, dict) and "spec" in spec_data:
            spec = load_spec(spec_data["spec"])
            result = await execute_agent(
                spec=spec,
                input_data=params.get("input") or {},
                model_override=params.get("model"),
            )
            result = {
                "output": result.output,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "cost_usd": result.cost_usd,
            }
        else:
            result = db_agent_run(db, **params)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, indent=2)
        return [TextContent(type="text", text=text)]
    except Exception:
        full_tb = traceback.format_exc()
        return [TextContent(type="text", text=f"[agent_run] FULL TRACEBACK:\n{full_tb}")]
