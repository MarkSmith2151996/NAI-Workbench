from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.agents import agent_runs

METADATA = {'description': 'Get run history for agents. Returns recent runs with status, tokens, cost, and output summary.', 'input_schema': {'properties': {'agent': {'description': 'Agent name or ID to filter by (optional — all agents if omitted)', 'type': 'string'}, 'limit': {'default': 10, 'description': 'Max runs to return (default 10)', 'type': 'integer'}}, 'type': 'object'}, 'name': 'agent_runs'}


async def handle(params: dict, db):
    result = agent_runs(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
