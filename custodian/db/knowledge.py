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

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

async def handle_lookup_symbol(args):
    """Live tree-sitter lookup — always-current line numbers."""
    project_name = args["project"]
    symbol_name = args["symbol"]
    exact = args.get("exact", False)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = _to_native_path(project["path"])
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    matches = _find_symbol(project_path, symbol_name, exact=exact)

    if not matches:
        return [TextContent(type="text", text=f"No symbols matching '{symbol_name}' found in {project_name}.")]

    # Limit to 50 results
    if len(matches) > 50:
        matches = matches[:50]
        truncated = True
    else:
        truncated = False

    result = {"matches": matches, "count": len(matches)}
    if truncated:
        result["note"] = "Results truncated to 50. Use exact=true for precise matches."

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_symbol_context(args):
    """Get Sonnet's description and relationships from the fossil DB."""
    project_name = args["project"]
    symbol_name = args["symbol"]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        # Get latest fossil's symbols
        symbols = conn.execute(
            """SELECT s.file_path, s.line_number, s.type, s.name, s.signature,
                      s.description, s.relationships
               FROM symbols s
               JOIN fossils f ON f.id = s.fossil_id
               WHERE s.project_id = ?
                 AND LOWER(s.name) LIKE LOWER(?)
               ORDER BY f.created_at DESC""",
            (project["id"], f"%{symbol_name}%"),
        ).fetchall()

    if not symbols:
        return [TextContent(
            type="text",
            text=f"No symbol context for '{symbol_name}' in {project_name}. "
                 "The fossil may not include this symbol, or no fossil exists yet.",
        )]

    results = [dict(s) for s in symbols[:20]]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]

async def handle_find_related_files(args):
    """Find files related to a symbol via relationship data."""
    project_name = args["project"]
    symbol_name = args["symbol"]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        # Get all symbols matching the name
        symbols = conn.execute(
            """SELECT s.file_path, s.name, s.relationships
               FROM symbols s
               JOIN fossils f ON f.id = s.fossil_id
               WHERE s.project_id = ?
                 AND LOWER(s.name) LIKE LOWER(?)
               ORDER BY f.created_at DESC""",
            (project["id"], f"%{symbol_name}%"),
        ).fetchall()

        related_files = set()
        direct_files = set()

        for sym in symbols:
            direct_files.add(sym["file_path"])
            if sym["relationships"]:
                try:
                    rels = json.loads(sym["relationships"])
                    # Collect all referenced symbols
                    referenced = set()
                    for key in ("calls", "called_by", "depends_on"):
                        referenced.update(rels.get(key, []))

                    # Look up file paths for referenced symbols
                    for ref_name in referenced:
                        ref_rows = conn.execute(
                            """SELECT DISTINCT s.file_path
                               FROM symbols s
                               JOIN fossils f ON f.id = s.fossil_id
                               WHERE s.project_id = ? AND s.name = ?
                               ORDER BY f.created_at DESC""",
                            (project["id"], ref_name),
                        ).fetchall()
                        for r in ref_rows:
                            related_files.add(r["file_path"])
                except (json.JSONDecodeError, TypeError):
                    pass

    result = {
        "direct_files": sorted(direct_files),
        "related_files": sorted(related_files - direct_files),
        "all_files": sorted(direct_files | related_files),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_recent_changes(args):
    project_name = args["project"]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        fossil = conn.execute(
            "SELECT recent_changes, created_at FROM fossils WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            (project["id"],),
        ).fetchone()

    if not fossil:
        return [TextContent(type="text", text=f"No fossil for '{project_name}'. Run trigger_custodian first.")]

    result = {
        "project": project_name,
        "fossil_date": fossil["created_at"],
        "recent_changes": fossil["recent_changes"],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_detective_insights(args):
    project_name = args.get("project")
    insight_type = args.get("insight_type")

    with db_connection() as conn:
        query = "SELECT * FROM detective_insights WHERE 1=1"
        params = []

        if project_name:
            project = get_project_by_name(conn, project_name)
            if project:
                query += " AND (project_id = ? OR project_id IS NULL)"
                params.append(project["id"])
        else:
            query += " AND project_id IS NULL"

        if insight_type:
            query += " AND insight_type = ?"
            params.append(insight_type)

        query += " ORDER BY created_at DESC LIMIT 20"
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return [TextContent(type="text", text="No detective insights found.")]

    results = [dict(r) for r in rows]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]

async def handle_trigger_custodian(args):
    project_name = args["project"]
    log_query("trigger_custodian", project_name)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Find the custodian CLI
    custodian_dir = CUSTODIAN_ROOT
    index_script = os.path.join(custodian_dir, "index_project.sh")

    if not os.path.exists(index_script):
        return [TextContent(type="text", text=f"Custodian index script not found at {index_script}")]

    try:
        # Create indexing run record + log file
        log_path = f"/tmp/custodian/indexing-{project['name']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        with db_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO indexing_runs (project_id, status, log_path, started_at)
                   VALUES (?, 'running', ?, datetime('now'))""",
                (project["id"], log_path),
            )
            run_id = cursor.lastrowid
            conn.commit()

        # Launch async — redirect output to log file instead of DEVNULL
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            ["bash", index_script, project["name"], _to_native_path(project["path"]), str(run_id)],
            cwd=custodian_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        # Close Python's copy of the FD — the subprocess owns it now
        log_file.close()
        with _background_procs_lock:
            _background_procs.append(proc)
        return [TextContent(
            type="text",
            text=f"Custodian indexing started for '{project_name}' (run #{run_id}). "
                 f"Log: {log_path}\n"
                 "Use get_project_fossil in a minute to check results.",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start custodian: {e}")]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def lookup_symbol(conn, **params):
    return _unwrap(await handle_lookup_symbol(params))


async def get_symbol_context(conn, **params):
    return _unwrap(await handle_get_symbol_context(params))


async def find_related_files(conn, **params):
    return _unwrap(await handle_find_related_files(params))


async def get_recent_changes(conn, **params):
    return _unwrap(await handle_get_recent_changes(params))


async def get_detective_insights(conn, **params):
    return _unwrap(await handle_get_detective_insights(params))


async def trigger_custodian(conn, **params):
    return _unwrap(await handle_trigger_custodian(params))
