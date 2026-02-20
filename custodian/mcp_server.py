#!/usr/bin/env python3
"""Custodian MCP Server — Exposes fossil data and live symbol queries to Claude.

8 tools:
- list_projects: All registered projects with status
- get_project_fossil: Latest architecture + summary + dependencies
- lookup_symbol: Live tree-sitter search (always-current line numbers)
- get_symbol_context: Sonnet's description + relationships from DB
- find_related_files: Files you'd touch to change a symbol
- get_recent_changes: Summarized recent commits
- get_detective_insights: Known patterns, warnings, coupling
- trigger_custodian: Run Sonnet indexing for a project
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")

# Import local symbol parser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_symbols import find_symbol


def get_db():
    """Get a SQLite connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def log_query(tool_name, project_name=None, params=None):
    """Log MCP tool usage for detective analysis."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO query_log (tool_name, project_name, query_params) VALUES (?, ?, ?)",
            (tool_name, project_name, json.dumps(params) if params else None),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Don't let logging failures break tool calls


def get_project_by_name(conn, name):
    """Look up a project by name (case-insensitive, partial match)."""
    # Try exact match first
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try case-insensitive
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)
    ).fetchone()
    if row:
        return row

    # Try partial match
    row = conn.execute(
        "SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'",
        (f"%{name}%",),
    ).fetchone()
    return row


# --- MCP Server Setup ---

app = Server("custodian")


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_projects",
            description="List all registered projects with their status, stack, and last indexed time.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_project_fossil",
            description="Get the latest fossil (architecture summary, file tree, dependencies, known issues) for a project. This is the fastest way to understand a project's structure without exploring files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name (e.g., 'progress-tracker', 'finance95', 'bjtrader', 'fba-command-center')",
                    },
                    "include_file_tree": {
                        "type": "boolean",
                        "description": "Include the full file tree (can be large). Default: false.",
                        "default": False,
                    },
                    "include_symbols": {
                        "type": "boolean",
                        "description": "Include full symbol list from this fossil. Default: false.",
                        "default": False,
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="lookup_symbol",
            description="Find a function, class, component, or type by name using live tree-sitter parsing. Returns CURRENT file paths and line numbers (not from fossil — always accurate). Use this to find where something is defined.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for (partial match supported)",
                    },
                    "exact": {
                        "type": "boolean",
                        "description": "Exact name match only. Default: false.",
                        "default": False,
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="get_symbol_context",
            description="Get Sonnet's description and relationship analysis for a known symbol. Unlike lookup_symbol (which gives current location), this gives semantic understanding from the last fossil.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name (partial match supported)",
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="find_related_files",
            description="Given a symbol or concept, find all files that would likely need changes. Uses relationship data from fossils.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name or concept to find related files for",
                    },
                },
                "required": ["project", "symbol"],
            },
        ),
        Tool(
            name="get_recent_changes",
            description="Get summarized recent commits for a project (from the latest fossil).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name",
                    },
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="get_detective_insights",
            description="Get known patterns, warnings, coupling analysis, and architectural insights for a project (or cross-project insights if no project specified).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name. Omit for cross-project insights.",
                    },
                    "insight_type": {
                        "type": "string",
                        "description": "Filter by type: coupling, growth, pattern, regression, prompt_refinement",
                    },
                },
            },
        ),
        Tool(
            name="trigger_custodian",
            description="Run Sonnet indexing for a specific project. Creates a new fossil. This is an async operation — results won't be immediate.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name to index",
                    },
                },
                "required": ["project"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "list_projects":
            return await handle_list_projects(arguments)
        elif name == "get_project_fossil":
            return await handle_get_fossil(arguments)
        elif name == "lookup_symbol":
            return await handle_lookup_symbol(arguments)
        elif name == "get_symbol_context":
            return await handle_get_symbol_context(arguments)
        elif name == "find_related_files":
            return await handle_find_related_files(arguments)
        elif name == "get_recent_changes":
            return await handle_get_recent_changes(arguments)
        elif name == "get_detective_insights":
            return await handle_get_detective_insights(arguments)
        elif name == "trigger_custodian":
            return await handle_trigger_custodian(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def handle_list_projects(args):
    log_query("list_projects")
    conn = get_db()
    rows = conn.execute(
        """SELECT p.name, p.path, p.stack, p.status, p.last_indexed,
                  COUNT(f.id) as fossil_count,
                  (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count
           FROM projects p
           LEFT JOIN fossils f ON f.project_id = p.id
           GROUP BY p.id
           ORDER BY p.name"""
    ).fetchall()
    conn.close()

    projects = []
    for row in rows:
        projects.append({
            "name": row["name"],
            "path": row["path"],
            "stack": row["stack"],
            "status": row["status"],
            "last_indexed": row["last_indexed"],
            "fossil_count": row["fossil_count"],
            "symbol_count": row["symbol_count"],
        })

    return [TextContent(type="text", text=json.dumps(projects, indent=2))]


async def handle_get_fossil(args):
    project_name = args["project"]
    include_tree = args.get("include_file_tree", False)
    include_symbols = args.get("include_symbols", False)

    log_query("get_project_fossil", project_name, args)
    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

    fossil = conn.execute(
        """SELECT * FROM fossils
           WHERE project_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (project["id"],),
    ).fetchone()

    if not fossil:
        conn.close()
        return [TextContent(type="text", text=f"No fossil found for '{project_name}'. Run trigger_custodian to create one.")]

    result = {
        "project": project["name"],
        "path": project["path"],
        "stack": project["stack"],
        "fossil_version": fossil["version"],
        "fossil_date": fossil["created_at"],
        "summary": fossil["summary"],
        "architecture": fossil["architecture"],
        "known_issues": fossil["known_issues"],
        "dependencies": fossil["dependencies"],
    }

    if include_tree:
        result["file_tree"] = fossil["file_tree"]

    if include_symbols:
        symbols = conn.execute(
            """SELECT file_path, line_number, type, name, signature, description, relationships
               FROM symbols WHERE fossil_id = ?
               ORDER BY file_path, line_number""",
            (fossil["id"],),
        ).fetchall()
        result["symbols"] = [dict(s) for s in symbols]

    conn.close()
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_lookup_symbol(args):
    """Live tree-sitter lookup — always-current line numbers."""
    project_name = args["project"]
    symbol_name = args["symbol"]
    exact = args.get("exact", False)

    log_query("lookup_symbol", project_name, {"symbol": symbol_name, "exact": exact})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    project_path = project["path"]
    if not os.path.isdir(project_path):
        return [TextContent(type="text", text=f"Project path not found: {project_path}")]

    matches = find_symbol(project_path, symbol_name, exact=exact)

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

    log_query("get_symbol_context", project_name, {"symbol": symbol_name})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
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
    conn.close()

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

    log_query("find_related_files", project_name, {"symbol": symbol_name})

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
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

    conn.close()

    result = {
        "direct_files": sorted(direct_files),
        "related_files": sorted(related_files - direct_files),
        "all_files": sorted(direct_files | related_files),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def handle_get_recent_changes(args):
    project_name = args["project"]
    log_query("get_recent_changes", project_name)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    if not project:
        conn.close()
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    fossil = conn.execute(
        "SELECT recent_changes, created_at FROM fossils WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    conn.close()

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

    log_query("get_detective_insights", project_name, {"insight_type": insight_type})

    conn = get_db()

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
    conn.close()

    if not rows:
        return [TextContent(type="text", text="No detective insights found.")]

    results = [dict(r) for r in rows]
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def handle_trigger_custodian(args):
    project_name = args["project"]
    log_query("trigger_custodian", project_name)

    conn = get_db()
    project = get_project_by_name(conn, project_name)
    conn.close()

    if not project:
        return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

    # Find the custodian CLI
    custodian_dir = os.path.dirname(os.path.abspath(__file__))
    index_script = os.path.join(custodian_dir, "index_project.sh")

    if not os.path.exists(index_script):
        return [TextContent(type="text", text=f"Custodian index script not found at {index_script}")]

    try:
        # Launch async — don't block the MCP call
        subprocess.Popen(
            ["bash", index_script, project["name"], project["path"]],
            cwd=custodian_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return [TextContent(
            type="text",
            text=f"Custodian indexing started for '{project_name}'. "
                 "Use get_project_fossil in a minute to check results.",
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to start custodian: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
