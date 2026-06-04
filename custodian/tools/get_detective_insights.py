from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.knowledge import get_detective_insights

METADATA = {'description': 'Get known patterns, warnings, coupling analysis, and architectural insights for a project (or cross-project insights if no project specified).', 'input_schema': {'properties': {'insight_type': {'description': 'Filter by type: coupling, growth, pattern, regression, prompt_refinement', 'type': 'string'}, 'project': {'description': 'Project name. Omit for cross-project insights.', 'type': 'string'}}, 'type': 'object'}, 'name': 'get_detective_insights'}


async def handle(params: dict, db):
    result = get_detective_insights(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
