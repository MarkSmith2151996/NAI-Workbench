from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.pipelines import get_pipeline_run

METADATA = {'description': 'Get a pipeline run summary with per-step status and foreach counts.', 'input_schema': {'properties': {'pipeline': {'description': 'Pipeline name if run_id is not provided.', 'type': 'string'}, 'run_id': {'description': 'Pipeline run ID.', 'type': 'integer'}, 'run_name': {'description': 'Run name when looking up by pipeline name.', 'type': 'string'}}, 'type': 'object'}, 'name': 'get_pipeline_run'}


async def handle(params: dict, db):
    result = get_pipeline_run(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
