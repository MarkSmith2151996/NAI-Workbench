from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.pipelines import invoke_pipeline

METADATA = {'description': 'Run a registered pipeline synchronously with validated input.', 'input_schema': {'properties': {'input': {'description': 'Invocation input matching the pipeline input_schema.', 'type': 'object'}, 'pipeline': {'description': 'Pipeline name or numeric ID.', 'type': 'string'}}, 'required': ['pipeline', 'input'], 'type': 'object'}, 'name': 'invoke_pipeline'}


async def handle(params: dict, db):
    result = invoke_pipeline(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
