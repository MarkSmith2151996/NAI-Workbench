from __future__ import annotations

import json
import os

from mcp.types import TextContent

from custodian.db.connection import db_connection


METADATA = {
    'name': 'read_project_status',
    'description': (
        'Read a project\'s STATUS.md — the living architecture and state document '
        'maintained by OpenCode after every task execution. Returns the full content. '
        'Use this to orient yourself on a project before planning, debugging, or reviewing. '
        'Replaces the fossil indexer for project context.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'project': {
                'type': 'string',
                'description': 'Custodian project name (lowercase), matching projects.name.',
            },
        },
        'required': ['project'],
    },
}


def _win_to_wsl(path: str) -> str:
    """Convert a Windows path to WSL /mnt/ form. Passes through WSL paths."""
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return path


async def handle(params: dict, db):
    project_name = params.get("project", "").strip()
    if not project_name:
        return [TextContent(type="text", text=json.dumps({"error": "project is required"}))]

    with db_connection() as conn:
        row = conn.execute(
            "SELECT path FROM projects WHERE name = ?", (project_name,)
        ).fetchone()

    if not row:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown project: {project_name}"}))]

    project_path = row[0]
    wsl_path = _win_to_wsl(project_path)
    status_file = os.path.join(wsl_path, "STATUS.md")

    if not os.path.isfile(status_file):
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": f"No STATUS.md found for project '{project_name}' at {status_file}",
                "hint": "Run a task with the STATUS.md update rule to bootstrap it, or create it manually.",
            }),
        )]

    try:
        with open(status_file, "r", errors="replace") as fh:
            content = fh.read()
        if len(content) > 50000:
            content = content[:50000] + "\n\n... [truncated at 50KB]"
        return [TextContent(type="text", text=content)]
    except Exception as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Failed to read STATUS.md: {exc}"}))]
