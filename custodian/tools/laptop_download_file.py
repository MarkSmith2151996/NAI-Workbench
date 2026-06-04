from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.laptop_bridge import download_file

METADATA = {'description': "Download a file from the REMOTE Mac to this PC over Tailscale. Use for binary files (images, archives) that can't transfer through JSON. remote_path is on the Mac, local_path is where to save on the PC.", 'input_schema': {'properties': {'local_path': {'description': "Absolute path to save locally on the PC (e.g., '/tmp/screenshot.png')", 'type': 'string'}, 'remote_path': {'description': "Absolute path on the REMOTE Mac (e.g., '/Users/<username>/screenshot.png')", 'type': 'string'}}, 'required': ['remote_path', 'local_path'], 'type': 'object'}, 'name': 'laptop_download_file'}


async def handle(params: dict, db):
    result = download_file(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
