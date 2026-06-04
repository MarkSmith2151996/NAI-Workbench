#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.util
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, ServerNotification, TextContent, Tool, ToolListChangedNotification

from custodian.core.legacy import cleanup_legacy_resources
from custodian.db.connection import db_connection
from custodian.db.migrations import run_all_migrations
from custodian.services.tool_router import set_mcp_registry
from custodian.session_registry import disconnect_session, ensure_registry, register_session, touch_session


TOOL_DIR = Path(__file__).resolve().parent.parent / "tools"
_TOOL_REGISTRY: dict[str, dict] = {}
_TOOL_LOAD_ERRORS: dict[str, str] = {}
_RUNTIME_INITIALIZED = False
_STDIO_SESSION_ID = contextvars.ContextVar("custodian_runtime_stdio_session_id", default=None)
_STDIO_WRITE_STREAM = None
_REGISTRY_LOCK = threading.RLock()
_WATCHER_THREAD: threading.Thread | None = None
_WATCHER_STOP = threading.Event()
_WATCHER_STATE: dict[str, float] = {}
_WATCHER_STARTED = False
_RUNTIME_LOOP: asyncio.AbstractEventLoop | None = None
_HTTP_SESSION_MANAGERS: set[object] = set()
_NOTIFY_LOCK = threading.Lock()

app = Server("custodian")
_ORIGINAL_CREATE_INITIALIZATION_OPTIONS = app.create_initialization_options


def _patched_create_initialization_options(notification_options=None, experimental_capabilities=None):
    if notification_options is None:
        notification_options = NotificationOptions(tools_changed=True)
    if experimental_capabilities is None:
        experimental_capabilities = {}
    return _ORIGINAL_CREATE_INITIALIZATION_OPTIONS(notification_options, experimental_capabilities)


app.create_initialization_options = _patched_create_initialization_options


def _get_current_tool_session_id() -> str | None:
    try:
        request = app.request_context.request
    except LookupError:
        request = None

    if request is not None:
        headers = getattr(request, "headers", None)
        if headers is not None:
            session_id = headers.get("mcp-session-id")
            if session_id:
                return session_id

    return _STDIO_SESSION_ID.get()


def _tool_file_state(tools_dir: Path = TOOL_DIR) -> dict[str, float]:
    state: dict[str, float] = {}
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        try:
            state[path.name] = path.stat().st_mtime
        except FileNotFoundError:
            continue
    return state


def load_tools(tools_dir: Path = TOOL_DIR) -> tuple[dict[str, dict], dict[str, str]]:
    registry: dict[str, dict] = {}
    errors: dict[str, str] = {}

    importlib.invalidate_caches()
    for path in sorted(tools_dir.glob("*.py")):
        if path.name in {"__init__.py", "_template.py"} or path.name.startswith("_"):
            continue

        module_name = f"custodian.tools.{path.stem}"
        try:
            if module_name in sys.modules:
                del sys.modules[module_name]
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"could not create import spec for {path.name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            metadata = getattr(module, "METADATA")
            handler = getattr(module, "handle")
            if not isinstance(metadata, dict):
                raise TypeError("METADATA must be a dict")
            for required_key in ("name", "description", "input_schema"):
                if required_key not in metadata:
                    raise ValueError(f"METADATA missing required field '{required_key}'")
            registry[metadata["name"]] = {"metadata": metadata, "handler": handler}
        except Exception as exc:  # pragma: no cover - startup diagnostics path
            errors[path.stem] = str(exc)

    return registry, errors


def register_http_session_manager(session_manager) -> None:
    with _NOTIFY_LOCK:
        _HTTP_SESSION_MANAGERS.add(session_manager)


def unregister_http_session_manager(session_manager) -> None:
    with _NOTIFY_LOCK:
        _HTTP_SESSION_MANAGERS.discard(session_manager)


def _tool_list_changed_message() -> SessionMessage:
    notification = ServerNotification(ToolListChangedNotification())
    jsonrpc_notification = JSONRPCNotification(
        jsonrpc="2.0",
        **notification.model_dump(by_alias=True, mode="json", exclude_none=True),
    )
    return SessionMessage(message=JSONRPCMessage(jsonrpc_notification))


async def _broadcast_tool_list_changed() -> None:
    message = _tool_list_changed_message()
    sent_streams: set[int] = set()

    if _STDIO_WRITE_STREAM is not None:
        stream_id = id(_STDIO_WRITE_STREAM)
        if stream_id not in sent_streams:
            try:
                await _STDIO_WRITE_STREAM.send(message)
                sent_streams.add(stream_id)
            except Exception:
                logging.getLogger("uvicorn.error").warning("[custodian] failed to notify stdio tool list change", exc_info=True)

    with _NOTIFY_LOCK:
        managers = list(_HTTP_SESSION_MANAGERS)

    for manager in managers:
        transports = getattr(manager, "_server_instances", {})
        for transport in list(transports.values()):
            write_stream = getattr(transport, "_write_stream", None)
            if write_stream is None:
                continue
            stream_id = id(write_stream)
            if stream_id in sent_streams:
                continue
            try:
                await write_stream.send(message)
                sent_streams.add(stream_id)
            except Exception:
                logging.getLogger("uvicorn.error").warning("[custodian] failed to notify HTTP tool list change", exc_info=True)


def _schedule_tool_list_changed_notification() -> None:
    if _RUNTIME_LOOP is None or _RUNTIME_LOOP.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_broadcast_tool_list_changed(), _RUNTIME_LOOP)


def reload_tool_registry(notify: bool = True) -> tuple[int, dict[str, str]]:
    global _TOOL_REGISTRY, _TOOL_LOAD_ERRORS, _WATCHER_STATE

    new_registry, errors = load_tools()
    with _REGISTRY_LOCK:
        _TOOL_REGISTRY = new_registry
        _TOOL_LOAD_ERRORS = errors
        _WATCHER_STATE = _tool_file_state()
    set_mcp_registry(new_registry)

    if errors:
        logging.getLogger("uvicorn.error").warning(
            "[custodian] tool reload: %s loaded, %s failed: %s",
            len(new_registry),
            len(errors),
            sorted(errors),
        )
    else:
        logging.getLogger("uvicorn.error").info("[custodian] tool reload: %s loaded, 0 failures", len(new_registry))

    if notify:
        _schedule_tool_list_changed_notification()

    return len(new_registry), dict(errors)


def _watch_tools_loop() -> None:
    logger = logging.getLogger("uvicorn.error")
    while not _WATCHER_STOP.wait(2.5):
        current_state = _tool_file_state()
        if current_state == _WATCHER_STATE:
            continue
        logger.info("[custodian] tools/ change detected, reloading tool registry")
        try:
            reload_tool_registry(notify=True)
        except Exception:
            logger.exception("[custodian] hot-reload failed; keeping previous registry")


def ensure_tool_watcher_started() -> None:
    global _WATCHER_THREAD, _WATCHER_STARTED, _WATCHER_STATE
    if _WATCHER_STARTED:
        return
    _WATCHER_STATE = _tool_file_state()
    _WATCHER_STOP.clear()
    _WATCHER_THREAD = threading.Thread(target=_watch_tools_loop, name="custodian-tool-watcher", daemon=True)
    _WATCHER_THREAD.start()
    _WATCHER_STARTED = True
    logging.getLogger("uvicorn.error").info("[custodian] Watching tools/ for changes — hot-reload enabled")


def stop_tool_watcher() -> None:
    global _WATCHER_THREAD, _WATCHER_STARTED
    _WATCHER_STOP.set()
    if _WATCHER_THREAD is not None:
        _WATCHER_THREAD.join(timeout=5)
    _WATCHER_THREAD = None
    _WATCHER_STARTED = False


def set_runtime_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _RUNTIME_LOOP
    _RUNTIME_LOOP = loop


def initialize_runtime(force: bool = False) -> None:
    global _TOOL_REGISTRY, _TOOL_LOAD_ERRORS, _RUNTIME_INITIALIZED

    if _RUNTIME_INITIALIZED and not force:
        return

    run_all_migrations()
    _TOOL_REGISTRY, _TOOL_LOAD_ERRORS = load_tools()
    _WATCHER_STATE = _tool_file_state()
    set_mcp_registry(_TOOL_REGISTRY)
    for module_name, error in _TOOL_LOAD_ERRORS.items():
        logging.getLogger("uvicorn.error").error("[custodian] failed to load tool module %s: %s", module_name, error)
    _RUNTIME_INITIALIZED = True
    ensure_tool_watcher_started()


def get_tool_load_failures() -> dict[str, str]:
    initialize_runtime()
    return dict(_TOOL_LOAD_ERRORS)


async def run_startup_healthcheck() -> dict:
    set_runtime_loop(asyncio.get_running_loop())
    initialize_runtime(force=True)

    if not _TOOL_REGISTRY:
        raise RuntimeError("No MCP tools were loaded.")

    if "list_projects" in _TOOL_REGISTRY:
        with db_connection() as conn:
            await _TOOL_REGISTRY["list_projects"]["handler"]({}, conn)

    return {
        "tool_count": len(_TOOL_REGISTRY),
        "failed_tools": dict(_TOOL_LOAD_ERRORS),
    }


@app.list_tools()
async def list_tools():
    initialize_runtime()
    with _REGISTRY_LOCK:
        entries = list(_TOOL_REGISTRY.values())
    return [
        Tool(
            name=entry["metadata"]["name"],
            description=entry["metadata"]["description"],
            inputSchema=entry["metadata"]["input_schema"],
        )
        for entry in entries
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    initialize_runtime()

    session_id = _get_current_tool_session_id()
    if session_id:
        touch_session(session_id)

    with _REGISTRY_LOCK:
        entry = _TOOL_REGISTRY.get(name)
    if entry is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        with db_connection() as conn:
            return await entry["handler"](arguments or {}, conn)
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]


async def main():
    import uuid

    loop = asyncio.get_event_loop()
    set_runtime_loop(loop)
    initialize_runtime(force=True)
    ensure_registry(reset_transport="stdio")
    stdio_session_id = uuid.uuid4().hex
    register_session(stdio_session_id, transport="stdio")
    session_token = _STDIO_SESSION_ID.set(stdio_session_id)

    def _shutdown(sig, _frame):
        print(f"[custodian] Received signal {sig}, shutting down...", file=sys.stderr)
        cleanup_legacy_resources()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        async with stdio_server() as (read_stream, write_stream):
            global _STDIO_WRITE_STREAM
            _STDIO_WRITE_STREAM = write_stream
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        _STDIO_WRITE_STREAM = None
        _STDIO_SESSION_ID.reset(session_token)
        disconnect_session(stdio_session_id)
        stop_tool_watcher()
        cleanup_legacy_resources()
