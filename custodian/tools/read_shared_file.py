from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.db.shared import read_shared_file

METADATA = {'description': 'Read a text file from the per-project shared folder. Path is relative to `/mnt/c/Users/Big A/custodian-shared/<project>/`. Access is restricted to the shared folder — paths escaping this boundary (via `..`, symlinks, absolute paths) are rejected. Text only — binary files return metadata with a clear error. Files over 5MB return metadata + first 100 lines truncated. Use this after a task executes to read files OpenCode produced, without user attachment.', 'input_schema': {'properties': {'limit': {'default': 2000, 'description': 'Max lines to return. Default: 2000.', 'type': 'integer'}, 'offset': {'default': 1, 'description': 'Start line (1-based). Default: 1.', 'type': 'integer'}, 'project': {'description': 'Custodian project name, lowercase, matching projects.name.', 'type': 'string'}, 'relative_path': {'description': "Path relative to the project's shared-folder root.", 'type': 'string'}}, 'required': ['project', 'relative_path'], 'type': 'object'}, 'name': 'read_shared_file'}


async def handle(params: dict, db):
    result = read_shared_file(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
