from __future__ import annotations

import inspect
import json

from mcp.types import TextContent
from custodian.services.sandbox import start

METADATA = {'description': "Start a sandbox process for a project. The sandbox runs inside a tmux session that the user's Sandbox widget auto-attaches to — the user SEES the program live in their terminal. IMPORTANT RULES: (1) When creating test programs, demos, or prototypes to DISPLAY in the sandbox, ALWAYS build terminal-based UIs using Python Textual, Rich, or curses — these render directly in the sandbox terminal and the user can see and interact with them immediately. NEVER create web servers (Flask, HTTP) for sandbox display — the user cannot see web pages in the terminal. (2) If the project needs Python packages (textual, rich, etc.), call sandbox_install FIRST. (3) Only use web mode (with port) for actual web projects (React, Next.js, Django) that already have a dev server — these will auto-open a Wave browser pane. (4) Do NOT pass a port unless the command actually starts a web server.", 'input_schema': {'properties': {'command': {'description': "Override command (e.g., 'npm run dev', 'python app.py'). Auto-detected if omitted.", 'type': 'string'}, 'port': {'description': 'Override port (implies web app type). Auto-detected if omitted.', 'type': 'integer'}, 'preview': {'default': True, 'description': 'Open Wave Terminal preview pane (default true).', 'type': 'boolean'}, 'project': {'description': 'Project name', 'type': 'string'}}, 'required': ['project'], 'type': 'object'}, 'name': 'sandbox_start'}


async def handle(params: dict, db):
    result = start(**params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
