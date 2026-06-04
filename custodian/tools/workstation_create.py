from __future__ import annotations

import inspect
import json

from mcp.types import TextContent

from custodian.services.workstations import create_spec, provision_instance


METADATA = {
    "name": "workstation_create",
    "description": "Create a workstation spec and provision its warm Docker runtime.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique workstation spec name."},
            "description": {"type": "string", "description": "Human-readable purpose."},
            "services": {"type": "array", "description": "Service definitions with name, host, and port.", "items": {"type": "object"}},
            "deps": {"type": "array", "description": "Packages to install. Strings install with pip; objects may specify manager/package.", "items": {}},
            "env_vars": {"type": "object", "description": "Environment variables to inject into the container."},
            "volumes": {"type": "array", "description": "Additional volume mounts.", "items": {}},
            "tool_definitions": {
                "type": "array",
                "description": "Tool definitions exposed to workstation agents.",
                "items": {"type": "object"},
            },
            "image": {"type": "string", "description": "Docker image to run.", "default": "nai-sandbox:latest"},
            "max_slots": {"type": "integer", "description": "Number of isolated slots.", "default": 10},
            "browser_profile": {"type": "string", "description": "Optional persistent browser profile path."},
        },
        "required": ["name", "description", "services"],
    },
}


async def handle(params: dict, db):
    spec = create_spec(
        name=params["name"],
        description=params.get("description"),
        services=params.get("services") or [],
        deps=params.get("deps") or [],
        env_vars=params.get("env_vars") or {},
        volumes=params.get("volumes") or [],
        tool_definitions=params.get("tool_definitions") or [],
        image=params.get("image") or "nai-sandbox:latest",
        max_slots=params.get("max_slots", 10),
        browser_profile=params.get("browser_profile"),
    )
    instance = provision_instance(spec["name"])
    if inspect.isawaitable(instance):
        instance = await instance
    return [TextContent(type="text", text=json.dumps({"spec": spec, **instance}, indent=2))]
