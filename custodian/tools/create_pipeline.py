from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.pipelines import create_pipeline

METADATA = {'description': "Create or update a YAML pipeline spec in Custodian's pipeline registry.", 'input_schema': {'properties': {'description': {'description': 'Optional description override.', 'type': 'string'}, 'name': {'description': 'Pipeline name, usually matching the YAML filename.', 'type': 'string'}, 'spec': {'description': 'Full YAML pipeline spec text.', 'type': 'string'}}, 'required': ['name', 'spec'], 'type': 'object'}, 'name': 'create_pipeline'}


async def handle(params: dict, db):
    result = create_pipeline(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
