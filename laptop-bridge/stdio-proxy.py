#!/usr/bin/env python3
"""Stdio-to-SSE proxy for the laptop bridge MCP server.

Claude Code connects to this as a stdio MCP server. This script
forwards all messages to the remote SSE-based laptop-bridge server
over HTTP, translating between stdio and SSE transports.
"""
import asyncio
import sys
import os

# Add the venv's site-packages
sys.path.insert(0, os.path.expanduser("~/.custodian-venv/lib/python3.12/site-packages"))

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client


BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://100.82.234.100:8222/sse")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")


async def main():
    headers = {}
    if BRIDGE_TOKEN:
        headers["Authorization"] = f"Bearer {BRIDGE_TOKEN}"

    async with sse_client(BRIDGE_URL, headers=headers) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Now act as a stdio server that proxies to the remote session
            # Read JSON-RPC from stdin, forward to remote, return responses to stdout
            import json

            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    # Forward the tool call to the remote session
                    if msg.get("method") == "tools/list":
                        result = await session.list_tools()
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "result": {"tools": [t.model_dump() for t in result.tools]}
                        }
                    elif msg.get("method") == "tools/call":
                        params = msg.get("params", {})
                        result = await session.call_tool(params["name"], params.get("arguments", {}))
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "result": {"content": [c.model_dump() for c in result.content]}
                        }
                    else:
                        response = {
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "result": {}
                        }
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
                except Exception as e:
                    err = {
                        "jsonrpc": "2.0",
                        "id": msg.get("id") if 'msg' in dir() else None,
                        "error": {"code": -1, "message": str(e)}
                    }
                    sys.stdout.write(json.dumps(err) + "\n")
                    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
