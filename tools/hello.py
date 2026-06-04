TOOL_NAME = "hello"
TOOL_DESCRIPTION = "Test tool that echoes back params"
TOOL_PARAMS = {
    "message": {"type": "string", "required": True, "description": "Message to echo"}
}


async def handler(params: dict) -> dict:
    return {"echo": params.get("message", ""), "source": "nai-workbench-box"}
