#!/usr/bin/env python3
"""Detective — Pattern analysis and prompt evolution for the Custodian system.

Analyses:
- Coupling detection: Files that always change together across fossils
- Growth tracking: Modules getting bigger over time
- Pattern repetition: Similar architectural choices across projects
- Regression detection: Patterns tried and abandoned
- Cross-project learning: Solutions reused or reinvented

Also handles prompt refinement based on MCP query logs.

Usage:
    detective.py --model sonnet [--project NAME]    # Run analysis
    detective.py --model opus [--project NAME]      # Deep analysis
    detective.py --refine-prompt                    # Analyze query gaps → refine prompt
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custodian.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def store_insight(project_id, fossil_id, insight_type, content, model_used, projects_involved=None):
    """Store a detective insight in the database."""
    conn = get_db()
    conn.execute(
        """INSERT INTO detective_insights
           (project_id, fossil_id, insight_type, content, model_used, projects_involved)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            fossil_id,
            insight_type,
            content,
            model_used,
            json.dumps(projects_involved) if projects_involved else None,
        ),
    )
    conn.commit()
    conn.close()


def build_analysis_context(project_name=None):
    """Build context for detective analysis from fossil history."""
    conn = get_db()

    context_parts = []

    if project_name:
        project = conn.execute(
            "SELECT * FROM projects WHERE name = ? OR LOWER(name) = LOWER(?)",
            (project_name, project_name),
        ).fetchone()
        if not project:
            print(f"Project '{project_name}' not found", file=sys.stderr)
            conn.close()
            return None, None

        projects = [dict(project)]
    else:
        projects = [dict(r) for r in conn.execute(
            "SELECT * FROM projects WHERE status = 'active'"
        ).fetchall()]

    for proj in projects:
        context_parts.append(f"\n=== PROJECT: {proj['name']} ===")
        context_parts.append(f"Path: {proj['path']}")
        context_parts.append(f"Stack: {proj['stack']}")

        # Get all fossils for this project
        fossils = conn.execute(
            """SELECT id, version, created_at, summary, architecture, known_issues, recent_changes, file_tree
               FROM fossils WHERE project_id = ?
               ORDER BY created_at ASC""",
            (proj["id"],),
        ).fetchall()

        if fossils:
            context_parts.append(f"\nFossil history ({len(fossils)} versions):")
            for f in fossils:
                context_parts.append(f"\n--- Fossil v{f['version']} ({f['created_at']}) ---")
                context_parts.append(f"Summary: {f['summary']}")
                if f['architecture']:
                    context_parts.append(f"Architecture: {f['architecture'][:500]}")
                if f['known_issues']:
                    context_parts.append(f"Known issues: {f['known_issues'][:500]}")
                if f['recent_changes']:
                    context_parts.append(f"Changes: {f['recent_changes'][:500]}")

                # File tree size changes
                try:
                    tree = json.loads(f['file_tree'] or '[]')
                    total_lines = sum(item.get('lines', 0) for item in tree if isinstance(item, dict))
                    context_parts.append(f"Total files: {len(tree)}, Total lines: {total_lines}")
                except (json.JSONDecodeError, TypeError):
                    pass

            # Symbol summary
            latest_fossil = fossils[-1]
            symbols = conn.execute(
                """SELECT type, COUNT(*) as count
                   FROM symbols WHERE fossil_id = ?
                   GROUP BY type ORDER BY count DESC""",
                (latest_fossil['id'],),
            ).fetchall()
            if symbols:
                sym_summary = ", ".join(f"{s['type']}: {s['count']}" for s in symbols)
                context_parts.append(f"Latest symbols: {sym_summary}")

    # Include query log analysis
    queries = conn.execute(
        """SELECT tool_name, project_name, query_params, COUNT(*) as count
           FROM query_log
           GROUP BY tool_name, project_name
           ORDER BY count DESC LIMIT 30"""
    ).fetchall()

    if queries:
        context_parts.append("\n=== MCP QUERY PATTERNS ===")
        for q in queries:
            context_parts.append(f"  {q['tool_name']} on {q['project_name'] or '*'}: {q['count']} calls")

    # Failed/empty queries (potential gaps)
    context_parts.append("\n=== RECENT QUERY DETAILS (last 50) ===")
    recent = conn.execute(
        "SELECT * FROM query_log ORDER BY timestamp DESC LIMIT 50"
    ).fetchall()
    for q in recent:
        context_parts.append(f"  [{q['timestamp']}] {q['tool_name']}({q['query_params'] or ''})")

    conn.close()

    project_id = projects[0]["id"] if len(projects) == 1 else None
    return "\n".join(context_parts), project_id


def run_analysis(model, project_name=None):
    """Run detective analysis using Claude."""
    context, project_id = build_analysis_context(project_name)
    if context is None:
        return

    prompt = """You are the Detective. Analyze the fossil history and query patterns below. Produce insights in JSON format.

For each insight, output a JSON object in an array:
[
  {
    "type": "coupling|growth|pattern|regression|prompt_refinement",
    "content": "detailed finding",
    "projects": ["project-name"]
  }
]

Analyze for:
1. COUPLING: Files that always change together (look at recent_changes across fossils)
2. GROWTH: Modules getting bigger over time (compare file line counts across fossil versions)
3. PATTERNS: Similar architectural choices repeated across projects (stores, data flow, etc.)
4. REGRESSION: Patterns tried and abandoned (things in earlier fossils but not later ones)
5. CROSS-PROJECT: Solutions in one project that could benefit another

Also analyze the MCP query patterns:
- What do users search for most?
- What queries might return empty results (gaps in fossil data)?
- What additional fields would make fossils more useful?

Output ONLY valid JSON array. No markdown fences."""

    print(f"Running {model} detective analysis...")
    print(f"Context size: {len(context)} chars")

    try:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            input=context,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            print(f"Claude failed: {result.stderr}", file=sys.stderr)
            return

        output = result.stdout.strip()

        # Try to parse JSON
        if output.startswith("```"):
            lines = output.split("\n")
            output = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            insights = json.loads(output)
        except json.JSONDecodeError:
            # Store as single raw insight
            print("Could not parse as JSON, storing as raw insight")
            store_insight(
                project_id=project_id,
                fossil_id=None,
                insight_type="pattern",
                content=output,
                model_used=model,
                projects_involved=[project_name] if project_name else None,
            )
            print("Stored 1 raw insight")
            return

        # Store each insight
        for insight in insights:
            if not isinstance(insight, dict):
                continue
            store_insight(
                project_id=project_id,
                fossil_id=None,
                insight_type=insight.get("type", "pattern"),
                content=insight.get("content", str(insight)),
                model_used=model,
                projects_involved=insight.get("projects"),
            )

        print(f"Stored {len(insights)} insights")

    except subprocess.TimeoutExpired:
        print("Analysis timed out (5 min limit)", file=sys.stderr)
    except FileNotFoundError:
        print("Claude CLI not found. Ensure 'claude' is in PATH.", file=sys.stderr)


def refine_prompt():
    """Analyze query logs to identify gaps and suggest prompt improvements."""
    conn = get_db()

    # Get current prompt
    current_prompt = conn.execute(
        "SELECT prompt FROM custodian_prompts WHERE project_id IS NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not current_prompt:
        print("No default prompt found", file=sys.stderr)
        conn.close()
        return

    # Get query patterns
    queries = conn.execute(
        """SELECT tool_name, query_params, COUNT(*) as count
           FROM query_log
           GROUP BY tool_name, query_params
           ORDER BY count DESC LIMIT 50"""
    ).fetchall()

    # Get symbol lookup patterns (what people search for)
    symbol_queries = conn.execute(
        """SELECT query_params FROM query_log
           WHERE tool_name = 'lookup_symbol'
           ORDER BY timestamp DESC LIMIT 30"""
    ).fetchall()

    conn.close()

    context = f"""Current custodian prompt:
{current_prompt['prompt']}

Query patterns (what Opus asks for):
"""
    for q in queries:
        context += f"  {q['tool_name']}: {q['query_params']} (×{q['count']})\n"

    context += "\nSymbol searches (what Opus looks up):\n"
    for sq in symbol_queries:
        context += f"  {sq['query_params']}\n"

    prompt = """Analyze the current custodian prompt and the MCP query patterns. The query patterns show what Claude Opus actually searches for when working on projects.

Identify:
1. Gaps: What Opus searches for that the fossil doesn't provide well
2. Improvements: How to restructure the prompt for better fossils
3. New fields: Any data that should be added to the fossil format

Output a JSON object:
{
    "analysis": "what you found",
    "suggested_prompt": "the complete improved custodian prompt",
    "changes": ["list of what changed and why"]
}

Output ONLY valid JSON."""

    print("Analyzing query patterns for prompt refinement...")

    try:
        result = subprocess.run(
            ["claude", "--model", "sonnet", "-p", prompt],
            input=context,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            print(f"Failed: {result.stderr}", file=sys.stderr)
            return

        output = result.stdout.strip()
        if output.startswith("```"):
            lines = output.split("\n")
            output = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            print("Could not parse refinement output as JSON")
            print(output[:500])
            return

        # Store the new prompt
        if "suggested_prompt" in data:
            conn = get_db()
            conn.execute(
                """INSERT INTO custodian_prompts
                   (project_id, prompt, created_by, notes)
                   VALUES (NULL, ?, 'detective', ?)""",
                (
                    data["suggested_prompt"],
                    json.dumps({
                        "analysis": data.get("analysis", ""),
                        "changes": data.get("changes", []),
                    }),
                ),
            )
            conn.commit()
            conn.close()
            print("Stored refined prompt")

            # Also store as an insight
            store_insight(
                project_id=None,
                fossil_id=None,
                insight_type="prompt_refinement",
                content=json.dumps(data, indent=2),
                model_used="sonnet",
            )
            print("Stored prompt refinement insight")

        if "changes" in data:
            print("\nChanges made:")
            for change in data["changes"]:
                print(f"  - {change}")

    except subprocess.TimeoutExpired:
        print("Prompt refinement timed out", file=sys.stderr)
    except FileNotFoundError:
        print("Claude CLI not found", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Custodian Detective — Pattern analysis and prompt evolution")
    parser.add_argument("--model", choices=["sonnet", "opus"], default="sonnet", help="Model to use")
    parser.add_argument("--project", "-p", help="Analyze a specific project (default: all)")
    parser.add_argument("--refine-prompt", action="store_true", help="Run prompt refinement instead of analysis")

    args = parser.parse_args()

    if args.refine_prompt:
        refine_prompt()
    else:
        run_analysis(args.model, args.project)


if __name__ == "__main__":
    main()
