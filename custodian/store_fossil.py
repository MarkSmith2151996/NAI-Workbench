#!/usr/bin/env python3
"""Parse Sonnet's JSON fossil output and store it in SQLite.

Usage:
    store_fossil.py <project_name> <json_file>
    store_fossil.py <project_name> -  # Read from stdin
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def store_fossil(project_name, fossil_json, prompt_used=None):
    """Parse and store a fossil from Sonnet's output."""
    conn = get_db()

    # Get project
    project = conn.execute(
        "SELECT * FROM projects WHERE name = ?", (project_name,)
    ).fetchone()
    if not project:
        # Try case-insensitive
        project = conn.execute(
            "SELECT * FROM projects WHERE LOWER(name) = LOWER(?)", (project_name,)
        ).fetchone()
    if not project:
        print(f"Error: Project '{project_name}' not found in database", file=sys.stderr)
        conn.close()
        return False

    project_id = project["id"]

    # Parse JSON
    if isinstance(fossil_json, str):
        # Try to extract JSON from markdown fences if present
        text = fossil_json.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            start = 1
            end = len(lines) - 1
            if lines[end].strip() == "```":
                text = "\n".join(lines[start:end])
            else:
                text = "\n".join(lines[start:])

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}", file=sys.stderr)
            print(f"First 500 chars: {text[:500]}", file=sys.stderr)
            conn.close()
            return False
    else:
        data = fossil_json

    # Get next version number
    last_version = conn.execute(
        "SELECT MAX(version) FROM fossils WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    version = (last_version or 0) + 1

    # If prompt wasn't provided, get the latest custodian prompt
    if prompt_used is None:
        prompt_row = conn.execute(
            """SELECT prompt FROM custodian_prompts
               WHERE project_id = ? OR project_id IS NULL
               ORDER BY project_id DESC, created_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        prompt_used = prompt_row["prompt"] if prompt_row else "unknown"

    # Helper: ensure value is a string (JSON-serialize lists/dicts)
    def to_text(val, default=""):
        if val is None:
            return default
        if isinstance(val, (list, dict)):
            return json.dumps(val, indent=2)
        return str(val)

    # Insert fossil
    cursor = conn.execute(
        """INSERT INTO fossils
           (project_id, version, file_tree, architecture, recent_changes,
            known_issues, dependencies, summary, prompt_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            version,
            json.dumps(data.get("file_tree", [])),
            to_text(data.get("architecture", "")),
            to_text(data.get("recent_changes", "")),
            to_text(data.get("known_issues", "")),
            json.dumps(data.get("dependencies", [])),
            data.get("summary", ""),
            prompt_used,
        ),
    )
    fossil_id = cursor.lastrowid

    # Insert symbols
    symbols = data.get("symbols", [])
    symbol_count = 0
    for sym in symbols:
        if not sym.get("name"):
            continue

        relationships = sym.get("relationships")
        if isinstance(relationships, dict):
            relationships = json.dumps(relationships)
        elif relationships is None:
            relationships = None

        conn.execute(
            """INSERT INTO symbols
               (project_id, fossil_id, file_path, line_number, type, name,
                signature, description, relationships)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                fossil_id,
                sym.get("file_path", sym.get("file", "")),
                sym.get("line_number", sym.get("line")),
                sym.get("type", "function"),
                sym["name"],
                sym.get("signature", ""),
                sym.get("description", ""),
                relationships,
            ),
        )
        symbol_count += 1

    # Update project last_indexed
    conn.execute(
        "UPDATE projects SET last_indexed = ? WHERE id = ?",
        (datetime.now().astimezone().isoformat(), project_id),
    )

    conn.commit()
    conn.close()

    print(f"Stored fossil v{version} for '{project_name}': {symbol_count} symbols")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: store_fossil.py <project_name> <json_file_or_->")
        sys.exit(1)

    project_name = sys.argv[1]
    source = sys.argv[2]

    if source == "-":
        fossil_text = sys.stdin.read()
    else:
        with open(source, "r", encoding="utf-8") as f:
            fossil_text = f.read()

    success = store_fossil(project_name, fossil_text)
    sys.exit(0 if success else 1)
