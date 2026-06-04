from __future__ import annotations

import collections
import json
import os
import platform as _platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import quote

from custodian.db.connection import DB_PATH, db_connection
from custodian.db.projects import (
    _read_text_file_preview,
    _read_text_file_window,
    _shared_project_root,
    _validate_shared_category,
    _validate_shared_relative_path,
)
from custodian.db.system import log_query
from mcp.types import TextContent

LIVE_FILE_TREE_ENTRY_LIMIT = 50
SHARED_FOLDER_ROOT = "/mnt/c/Users/Big A/custodian-shared"
SHARED_FOLDER_MAX_BYTES = 5 * 1024 * 1024
SHARED_FOLDER_BINARY_SNIFF_BYTES = 8192
SHARED_FOLDER_OVERSIZE_PREVIEW_LINES = 100
SHARED_FOLDER_CATEGORY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _check_wsl():
    if _platform.system() != "Linux" or not os.path.exists("/proc/version"):
        return False
    with open("/proc/version") as f:
        return "microsoft" in f.read().lower()


_IS_WSL = _check_wsl()
CUSTODIAN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _to_native_path(path):
    if not _IS_WSL or not path:
        return path
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def _find_symbol(*args, **kwargs):
    try:
        from custodian.parse_symbols import find_symbol
    except ImportError:
        from parse_symbols import find_symbol
    return find_symbol(*args, **kwargs)


def get_project_by_name(conn, name):
    row = conn.execute("SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'", (f"%{name}%",)).fetchone()

import mimetypes

async def handle_create_shared_folder(args):
    project_name = str(args.get("project") or "").strip()
    log_query("create_shared_folder", project_name or None, args)

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]

    try:
        category = _validate_shared_category(args.get("category"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

    path = os.path.join(_shared_project_root(project["name"]), category, "")
    created = not os.path.isdir(path)

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return [TextContent(type="text", text=f"Error creating shared folder: {exc}")]

    result = {
        "project": project["name"],
        "category": category,
        "path": path,
        "created": created,
        "note": "For persistent, discoverable folders, use setup_project_folder.",
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_read_shared_file(args):
    project_name = str(args.get("project") or "").strip()

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]

    try:
        relative_path = _validate_shared_relative_path(args.get("relative_path"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

    project_root = _shared_project_root(project["name"])
    absolute_path = os.path.join(project_root, relative_path)
    real_root = os.path.realpath(project_root)
    real_path = os.path.realpath(absolute_path)

    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        return [TextContent(type="text", text="Path escapes shared folder boundary.")]

    if os.path.isdir(real_path):
        entries = sorted(os.listdir(real_path))
        return [TextContent(type="text", text=json.dumps({
            "project": project["name"],
            "relative_path": relative_path,
            "absolute_path": real_path,
            "directory": True,
            "entries": entries,
        }, indent=2))]

    if not os.path.isfile(real_path):
        return [TextContent(type="text", text=f"File not found: '{relative_path}'. Did the task complete successfully?")]

    size_bytes = os.path.getsize(real_path)
    mime_guess = mimetypes.guess_type(real_path)[0] or "application/octet-stream"

    with open(real_path, "rb") as f:
        sample = f.read(SHARED_FOLDER_BINARY_SNIFF_BYTES)

    if b"\x00" in sample:
        result = {
            "project": project["name"],
            "relative_path": relative_path,
            "absolute_path": real_path,
            "size_bytes": size_bytes,
            "mime_type": mime_guess,
            "binary": True,
            "error": "binary files unsupported",
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        result = {
            "project": project["name"],
            "relative_path": relative_path,
            "absolute_path": real_path,
            "size_bytes": size_bytes,
            "mime_type": mime_guess,
            "binary": True,
            "error": "binary files unsupported",
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    try:
        if size_bytes > SHARED_FOLDER_MAX_BYTES:
            preview = _read_text_file_preview(real_path, SHARED_FOLDER_OVERSIZE_PREVIEW_LINES)
            result = {
                "project": project["name"],
                "relative_path": relative_path,
                "absolute_path": real_path,
                "size_bytes": size_bytes,
                "total_lines": preview["total_lines"],
                "lines_returned": preview["lines_returned"],
                "offset": preview["offset"],
                "content": preview["content"],
                "truncated": True,
                "reason": "file exceeds 5MB cap",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        window = _read_text_file_window(
            real_path,
            offset=args.get("offset", 1),
            limit=args.get("limit", 2000),
        )
    except OSError as exc:
        return [TextContent(type="text", text=f"Error reading shared file: {exc}")]

    result = {
        "project": project["name"],
        "relative_path": relative_path,
        "absolute_path": real_path,
        "size_bytes": size_bytes,
        "total_lines": window["total_lines"],
        "lines_returned": window["lines_returned"],
        "offset": window["offset"],
        "content": window["content"],
        "truncated": window["truncated"],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def create_shared_folder(conn, **params):
    return _unwrap(await handle_create_shared_folder(params))


async def read_shared_file(conn, **params):
    return _unwrap(await handle_read_shared_file(params))
