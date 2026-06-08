from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.db.transports import submit_transport


METADATA = {
    "name": "submit_transport",
    "description": "Package context from this Claude session for another Claude session to pick up. Returns a CS-NNN ID the user can reference in any other Claude session connected to Custodian. Use when the user says 'transport this', 'hand this off', 'send this to the other session', 'put this in transport', or when wrapping up a session that another session will continue.",
    "input_schema": {
        "type": "object",
        "required": ["title", "body"],
        "properties": {
            "title": {
                "type": "string",
                "description": "One-line summary of what this transport contains",
            },
            "body": {
                "type": "string",
                "description": "Full markdown context payload - decisions made, current state, next steps, anything the receiving session needs to continue",
            },
            "source_project": {
                "type": "string",
                "description": "Project this context came from",
            },
            "target_project": {
                "type": "string",
                "description": "Intended destination project, if known",
            },
        },
    },
}


async def handle(params: dict, db):
    result = submit_transport(db, **params)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return [TextContent(type="text", text=text)]
