#!/usr/bin/env python3
"""Initialize the Custodian database and seed default data."""

import sqlite3
import os
import json

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

DEFAULT_CUSTODIAN_PROMPT = """You are the Custodian. You are given a complete codebase dump, a symbol index, and recent git history for a project. Produce a JSON fossil with these fields:

- `file_tree`: array of {path, description (one line), lines (count)} for every significant file (skip node_modules, .git, dist, build, __pycache__, .next)
- `architecture`: how the major components connect (data flow, dependencies, entry points). Be specific about which files talk to which.
- `recent_changes`: summarize the last 20 commits — what changed and why. Group related commits.
- `known_issues`: any TODOs, FIXMEs, hacks, or tech debt you can identify. Include file paths.
- `dependencies`: array of {name, version, purpose} for key dependencies (from package.json, requirements.txt, etc.)
- `summary`: one paragraph describing what this project is, its current state, and how it works
- `symbols`: array of {file_path, line_number, type, name, signature, description, relationships} for every important function/class/component/hook/store/type. For relationships, include {calls: [], called_by: [], depends_on: []} where you can determine them.

Types for symbols: function, class, component, route, hook, store, type, interface, enum, constant

Output ONLY valid JSON. No markdown fences, no commentary. The JSON should have these top-level keys:
file_tree, architecture, recent_changes, known_issues, dependencies, summary, symbols"""

# Known projects to seed
PROJECTS = [
    {
        "name": "progress-tracker",
        "path": "C:\\Users\\Big A\\Progress-temp",
        "stack": "Next.js 14 + React 18 + Electron 40 + Supabase + Zustand + react95 + styled-components",
    },
    {
        "name": "finance95",
        "path": "E:\\Downloads\\finance95-v2",
        "stack": "Electron 40 + Vite 5 + React 18 + react95 + styled-components + @actual-app/api + Zustand",
    },
    {
        "name": "bjtrader",
        "path": "D:\\UserFolders\\Desktop\\BjTrader",
        "stack": "Python + Textual TUI + LangGraph + Claude CLI + Anthropic SDK + SQLite",
    },
    {
        "name": "fba-command-center",
        "path": "D:\\UserFolders\\Desktop\\Comand And Control\\fba_v2_backup",
        "stack": "Python + tkinter + Claude CLI + SQLite + MCP SDK",
    },
    {
        "name": "nai-workbench",
        "path": "C:\\Users\\Big A\\NAI-Workbench",
        "stack": "Python + Textual TUI + MCP SDK + SQLite + tree-sitter",
    },
]


def init_db():
    """Create database and tables from schema."""
    db_exists = os.path.exists(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Read and execute schema
    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()
    conn.executescript(schema)

    if not db_exists:
        print(f"Created database at {DB_PATH}")

    # Seed projects (upsert)
    for proj in PROJECTS:
        conn.execute(
            """INSERT INTO projects (name, path, stack)
               VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   path = excluded.path,
                   stack = excluded.stack""",
            (proj["name"], proj["path"], proj["stack"]),
        )
    print(f"Seeded {len(PROJECTS)} projects")

    # Seed default custodian prompt if none exists
    existing = conn.execute(
        "SELECT COUNT(*) FROM custodian_prompts WHERE project_id IS NULL"
    ).fetchone()[0]
    if existing == 0:
        conn.execute(
            """INSERT INTO custodian_prompts (project_id, prompt, created_by, notes)
               VALUES (NULL, ?, 'initial', 'Default custodian prompt')""",
            (DEFAULT_CUSTODIAN_PROMPT,),
        )
        print("Seeded default custodian prompt")

    conn.commit()
    conn.close()
    print("Database initialized successfully")


if __name__ == "__main__":
    init_db()
