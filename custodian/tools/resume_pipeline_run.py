from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.pipelines import resume_pipeline_run

METADATA = {'description': 'Resume a failed or paused pipeline run from its failed step, paused gate, or a specified step.', 'input_schema': {'properties': {'from_step': {'description': 'Optional step name to resume from.', 'type': 'string'}, 'input': {'description': 'Optional input payload to satisfy a paused human_gate step.', 'type': 'object'}, 'run_id': {'description': 'Pipeline run ID.', 'type': 'integer'}}, 'required': ['run_id'], 'type': 'object'}, 'name': 'resume_pipeline_run'}


async def handle(params: dict, db):
    result = resume_pipeline_run(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
