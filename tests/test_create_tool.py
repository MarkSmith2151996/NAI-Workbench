from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest


def _load_mcp_server_module():
    repo_root = Path(__file__).resolve().parents[1]
    custodian_dir = repo_root / "custodian"
    if str(custodian_dir) not in sys.path:
        sys.path.insert(0, str(custodian_dir))
    if "parse_symbols" not in sys.modules:
        stub = types.ModuleType("parse_symbols")
        stub.find_symbol = lambda *args, **kwargs: []
        sys.modules["parse_symbols"] = stub

    spec = importlib.util.spec_from_file_location("custodian_mcp_server_test", custodian_dir / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _create_test_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            stack TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_indexed TEXT,
            task_prefix TEXT
        );
        CREATE TABLE fossils (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL
        );
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL
        );
        CREATE TABLE tool_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            project TEXT NOT NULL,
            description TEXT,
            source_module TEXT NOT NULL,
            source_class TEXT,
            source_method TEXT,
            hook_point TEXT NOT NULL,
            return_type TEXT NOT NULL,
            known_side_effects TEXT,
            wrapper_path TEXT NOT NULL,
            input_schema TEXT,
            output_schema TEXT,
            handler_code TEXT,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            created_by TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tool_name, project)
        );
        """
    )
    conn.execute(
        "INSERT INTO projects (id, name, path, stack, status) VALUES (1, 'nai-workbench', '/workspace', 'Python', 'active')"
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_mcp_server_module()
    db_path = _create_test_db(tmp_path / "custodian-test.db")
    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "_ensure_project_box", lambda project: {"container_name": f"alpha-{project['name']}"})
    monkeypatch.setattr(module, "_ensure_box_running", lambda project, box: {**box, "tool_server_port": 9100})
    monkeypatch.setattr(module, "_ensure_box_tool_server", lambda project, box: 9100)
    return module


def _parse_tool_response(response):
    assert response and len(response) == 1
    return json.loads(response[0].text)


def test_create_tool_writes_handler(mcp_server, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_write(container_name, handler_path, handler_code):
        captured["container_name"] = container_name
        captured["handler_path"] = handler_path
        captured["handler_code"] = handler_code

    monkeypatch.setattr(mcp_server, "_write_tool_file_to_box", fake_write)
    monkeypatch.setattr(mcp_server, "_verify_box_tool_module", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_reload_tool_server", lambda *args: True)

    response = asyncio.run(
        mcp_server.handle_create_tool(
            {
                "name": "fetch_page",
                "project": "nai-workbench",
                "description": "Fetch a page",
                "input_schema": {"url": {"type": "string", "required": True}},
                "output_schema": {"content": {"type": "string"}},
                "handler_code": "def handle(params):\n    return {'ok': True}\n",
            }
        )
    )

    payload = _parse_tool_response(response)
    assert captured["container_name"] == "alpha-nai-workbench"
    assert captured["handler_path"] == "/workspace/tools/fetch_page.py"
    assert "def handle(params):" in captured["handler_code"]
    assert payload["status"] == "created"


def test_create_tool_registers_in_db(mcp_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_write_tool_file_to_box", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_verify_box_tool_module", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_reload_tool_server", lambda *args: False)

    asyncio.run(
        mcp_server.handle_create_tool(
            {
                "name": "fetch_page",
                "project": "nai-workbench",
                "description": "Fetch a page",
                "input_schema": {"url": {"type": "string", "required": True, "description": "URL to fetch"}},
                "output_schema": {"content": {"type": "string", "description": "Page content"}},
                "handler_code": "def handle(params):\n    return {'ok': True}\n",
            }
        )
    )

    conn = sqlite3.connect(mcp_server.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tool_registry WHERE tool_name = 'fetch_page' AND project = 'nai-workbench'").fetchone()
    conn.close()

    assert row is not None
    assert row["description"] == "Fetch a page"
    assert json.loads(row["input_schema"])["url"]["description"] == "URL to fetch"
    assert json.loads(row["output_schema"])["content"]["type"] == "string"
    assert row["handler_code"].startswith("def handle(params):")
    assert row["version"] == 1
    assert row["status"] == "active"


def test_create_tool_validates_handle_function(mcp_server):
    response = asyncio.run(
        mcp_server.handle_create_tool(
            {
                "name": "bad_tool",
                "project": "nai-workbench",
                "handler_code": "def nope(params):\n    return {}\n",
            }
        )
    )
    assert "handler_code must define a 'def handle(params)' function" in response[0].text


def test_update_tool_increments_version(mcp_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_write_tool_file_to_box", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_verify_box_tool_module", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_reload_tool_server", lambda *args: True)

    asyncio.run(
        mcp_server.handle_create_tool(
            {
                "name": "fetch_page",
                "project": "nai-workbench",
                "description": "Fetch a page",
                "input_schema": {"url": {"type": "string", "required": True}},
                "output_schema": {"content": {"type": "string"}},
                "handler_code": "def handle(params):\n    return {'ok': True}\n",
            }
        )
    )
    response = asyncio.run(
        mcp_server.handle_update_tool(
            {
                "name": "fetch_page",
                "project": "nai-workbench",
                "description": "Fetch a page, updated",
                "handler_code": "def handle(params):\n    return {'ok': 'updated'}\n",
            }
        )
    )

    payload = _parse_tool_response(response)
    assert payload["version"] == 2

    conn = sqlite3.connect(mcp_server.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT version, description, handler_code FROM tool_registry WHERE tool_name = 'fetch_page'").fetchone()
    conn.close()
    assert row["version"] == 2
    assert row["description"] == "Fetch a page, updated"
    assert "updated" in row["handler_code"]


def test_get_tool_returns_schemas(mcp_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_server, "_write_tool_file_to_box", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_verify_box_tool_module", lambda *args: None)
    monkeypatch.setattr(mcp_server, "_reload_tool_server", lambda *args: True)

    asyncio.run(
        mcp_server.handle_create_tool(
            {
                "name": "fetch_page",
                "project": "nai-workbench",
                "description": "Fetch a page",
                "input_schema": {"url": {"type": "string", "required": True}},
                "output_schema": {"content": {"type": "string"}},
                "handler_code": "def handle(params):\n    return {'ok': True}\n",
            }
        )
    )
    response = asyncio.run(mcp_server.handle_get_tool({"name": "fetch_page", "project": "nai-workbench"}))
    payload = _parse_tool_response(response)
    assert payload["input_schema"]["url"]["required"] is True
    assert payload["output_schema"]["content"]["type"] == "string"


def test_register_project_creates_project(mcp_server):
    response = asyncio.run(
        mcp_server.handle_register_project(
            {
                "name": "finance95-web",
                "path": "/workspace/finance95-web",
                "stack": "Vite + React 18 + react95",
            }
        )
    )

    payload = _parse_tool_response(response)
    assert payload == {
        "name": "finance95-web",
        "path": "/workspace/finance95-web",
        "stack": "Vite + React 18 + react95",
        "status": "active",
        "last_indexed": None,
        "fossil_count": 0,
        "symbol_count": 0,
    }


def test_register_project_rejects_duplicate_name_case_insensitive(mcp_server):
    response = asyncio.run(
        mcp_server.handle_register_project(
            {
                "name": "NAI-Workbench",
                "path": "/workspace/other",
            }
        )
    )

    assert response and "already exists" in response[0].text


def test_register_project_is_exposed_in_tool_list(mcp_server):
    tools = asyncio.run(mcp_server.list_tools())
    assert any(tool.name == "register_project" for tool in tools)


def test_compiler_uses_rich_schemas(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from custodian.compiler import compile_prompt

    db_path = _create_test_db(tmp_path / "compiler.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO tool_registry (
            tool_name, project, description, source_module, hook_point, return_type, wrapper_path,
            input_schema, output_schema, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "fetch_page",
            "nai-workbench",
            "Fetch URL content as plain text",
            "tools/fetch_page.py",
            "handle(params)",
            "dict",
            "tools/fetch_page.py",
            json.dumps(
                {
                    "url": {"type": "string", "required": True, "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "required": False, "description": "Max characters to return"},
                }
            ),
            json.dumps({"content": {"type": "string"}}),
            "active",
        ),
    )
    conn.commit()

    prompt = compile_prompt(
        system_prompt="Use tools well.",
        tools=[{"name": "fetch_page", "project": "nai-workbench"}],
        input_data={"topic": "test"},
        db=conn,
    )
    conn.close()

    assert "fetch_page: Fetch URL content as plain text" in prompt["system"]
    assert "url (string, required): URL to fetch" in prompt["system"]
    assert "max_chars (integer, optional): Max characters to return" in prompt["system"]
