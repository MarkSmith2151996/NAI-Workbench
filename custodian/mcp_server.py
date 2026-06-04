#!/usr/bin/env python3
"""Custodian MCP server entrypoint backed by the modular core runtime."""

from custodian.core.server import app, call_tool, list_tools, main


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
