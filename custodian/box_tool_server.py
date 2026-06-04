#!/usr/bin/env python3
"""Minimal HTTP tool server for project boxes.

Discovers Python tool modules in /workspace/tools and exposes them over a
small stdlib-only HTTP API.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


TOOLS_DIR = Path("/workspace/tools")
DEFAULT_PORT = 9100


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def load(self) -> None:
        self._tools.clear()
        if not TOOLS_DIR.is_dir():
            print(f"[box-tool-server] tools dir missing: {TOOLS_DIR}", file=sys.stderr)
            return

        for path in sorted(TOOLS_DIR.glob("*.py")):
            if path.name == "__init__.py" or path.name.startswith("_"):
                continue
            self._load_file(path)

    def _load_file(self, path: Path) -> None:
        module_name = f"box_tool_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError("unable to create module spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"[box-tool-server] failed to import {path.name}: {exc}", file=sys.stderr)
            return

        tool_name = getattr(module, "TOOL_NAME", None) or path.stem
        description = getattr(module, "TOOL_DESCRIPTION", None) or f"Tool loaded from {path.name}"
        params = getattr(module, "TOOL_PARAMS", None)
        if not isinstance(params, dict):
            params = {}
        handler = getattr(module, "handler", None) or getattr(module, "handle", None)

        if not tool_name or not callable(handler):
            print(
                f"[box-tool-server] skipping {path.name}: missing callable handler/handle",
                file=sys.stderr,
            )
            return

        self._tools[str(tool_name)] = {
            "name": str(tool_name),
            "description": str(description),
            "params": params,
            "handler": handler,
        }

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "params": tool["params"],
            }
            for tool in self._tools.values()
        ]

    def get(self, name: str) -> dict | None:
        return self._tools.get(name)


REGISTRY = ToolRegistry()


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _run_handler(handler, params: dict):
    result = handler(params)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


class ToolRequestHandler(BaseHTTPRequestHandler):
    server_version = "BoxToolServer/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_json(200, {"status": "ok", "tools": len(REGISTRY.list_tools())})
            return
        if self.path == "/tools":
            self._write_json(200, REGISTRY.list_tools())
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/reload":
            REGISTRY.load()
            self._write_json(200, {"status": "reloaded", "tools": len(REGISTRY.list_tools())})
            return
        prefix = "/tools/"
        if not self.path.startswith(prefix):
            self._write_json(404, {"error": "not found"})
            return

        tool_name = unquote(self.path[len(prefix):])
        tool = REGISTRY.get(tool_name)
        if tool is None:
            self._write_json(404, {"error": f"tool not found: {tool_name}"})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            self._write_json(400, {"error": "invalid JSON"})
            return

        if not isinstance(body, dict):
            self._write_json(400, {"error": "invalid JSON"})
            return

        try:
            result = _run_handler(tool["handler"], body)
            self._write_json(200, {"result": result})
        except Exception as exc:
            self._write_json(500, {"error": str(exc)})

    def log_message(self, format: str, *args) -> None:
        print(f"[box-tool-server] {self.address_string()} - {format % args}", file=sys.stderr)

    def _write_json(self, status: int, payload) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    REGISTRY.load()
    port = int(os.environ.get("BOX_TOOL_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("0.0.0.0", port), ToolRequestHandler)
    print(f"[box-tool-server] listening on {port} with {len(REGISTRY.list_tools())} tools", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
