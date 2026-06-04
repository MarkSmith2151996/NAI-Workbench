from __future__ import annotations

import collections
import json
import os
import platform as _platform
import re
import sqlite3
import subprocess
from datetime import datetime
import yaml

from custodian.db.connection import DB_PATH, db_connection
from custodian.db.system import log_query
from custodian.db.tools_registry import _ensure_box_running, _ensure_project_box
from custodian.agents.executor import execute_agent
from custodian.agents.schema import LlmAgentSpec
from custodian.agents.spec_loader import load_spec
from mcp.types import TextContent

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

import logging
from custodian.pipeline import DEFAULT_OUTPUT_BASE, PipelineError, PipelinePaused, PipelineRun, PipelineSpec
from custodian.services.opencode import OpenCodeRunnerError, list_available_models, run_opencode

def _validate_agent_model_name(model):
    """Validate an OpenAI model ID against OpenCode's live model list."""
    try:
        available = list_available_models()
    except OpenCodeRunnerError as e:
        return None, (
            "Error: could not validate model — OpenCode model list unavailable "
            f"({e}). Try again in a moment."
        )

    if model not in available:
        return None, (
            f"Error: model '{model}' is not currently available.\n"
            f"Available models:\n  " + "\n  ".join(available)
        )

    return available, None

def _list_yaml_agent_specs():
    result = subprocess.run(
        ["docker", "exec", "alpha-agentic-factory", "ls", "/workspace/agents/"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}

def _register_yaml_backed_agents(conn):
    project = get_project_by_name(conn, "agentic-factory")
    if not project:
        return

    available_specs = _list_yaml_agent_specs()
    if not available_specs:
        return

    desired_specs = {
        "data-runner": "agents/data-runner.yaml",
        "data-runner-test": "agents/data-runner-test.yaml",
        "brand-amazon-screener": "agents/brand-amazon-screener.yaml",
    }
    placeholder_prompt = "Agent behavior is defined in the YAML spec referenced by spec_path."

    for agent_name, spec_path in desired_specs.items():
        if os.path.basename(spec_path) not in available_specs:
            continue

        existing = conn.execute("SELECT id FROM agents WHERE name = ?", (agent_name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE agents SET project_id = ?, spec_path = ?, updated_at = ? WHERE id = ?",
                (project["id"], spec_path, datetime.now().isoformat(), existing["id"]),
            )
            continue

        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO agents (
                   name, description, system_prompt, model, project_id, max_turns,
                   spec_path, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                agent_name,
                "YAML-backed agent registry entry.",
                placeholder_prompt,
                "openai/gpt-5.4",
                project["id"],
                20,
                spec_path,
                now,
                now,
            ),
        )

def _normalize_agent_spec_path(spec_path):
    normalized = str(spec_path or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("Agent has no spec_path registered")
    normalized = os.path.normpath(normalized)
    if normalized.startswith("../") or normalized == ".." or os.path.isabs(normalized):
        raise ValueError("spec_path must stay within the project box /workspace root")
    return normalized

def _load_agent_spec(conn, agent_row):
    spec_path = agent_row["spec_path"]
    if not spec_path:
        raise ValueError("Agent has no spec_path registered")
    if not agent_row["project_id"]:
        raise ValueError("Agent is not bound to a project")

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (agent_row["project_id"],)).fetchone()
    if not project:
        raise ValueError("Agent project not found")

    box = _ensure_project_box(project)
    box = _ensure_box_running(project, box)
    normalized_spec_path = _normalize_agent_spec_path(spec_path)
    container_spec_path = f"/workspace/{normalized_spec_path}"

    result = subprocess.run(
        ["docker", "exec", box["container_name"], "cat", container_spec_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "docker exec cat failed").strip()
        raise FileNotFoundError(error)

    return project, normalized_spec_path, yaml.safe_load(result.stdout) or {}

def _resolve_agent(conn, identifier):
    """Look up an agent by name or ID. Returns Row or None."""
    # Try by ID first
    try:
        aid = int(identifier)
        row = conn.execute("SELECT * FROM agents WHERE id = ? AND status = 'active'", (aid,)).fetchone()
        if row:
            return row
    except (ValueError, TypeError):
        pass
    # Try exact name
    row = conn.execute(
        "SELECT * FROM agents WHERE name = ? AND status = 'active'", (identifier,)
    ).fetchone()
    if row:
        return row
    # Case-insensitive
    row = conn.execute(
        "SELECT * FROM agents WHERE LOWER(name) = LOWER(?) AND status = 'active'", (identifier,)
    ).fetchone()
    return row


def _ensure_agent_workstation_column(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    if "workstation" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN workstation TEXT")
        conn.commit()


def _validate_workstation(conn, name):
    workstation = str(name or "").strip() or None
    if not workstation:
        return None, None
    from custodian.services.workstations import get_spec

    spec = get_spec(workstation)
    if not spec or spec.get("status") != "active":
        return None, f"Error: workstation '{workstation}' not found or not active."
    return workstation, None

async def handle_agent_list(args):
    """List all agents."""
    status = args.get("status", "active")

    with db_connection() as conn:
        _ensure_agent_workstation_column(conn)
        rows = conn.execute(
            """SELECT a.*, p.name as project_name,
                      (SELECT COUNT(*) FROM agent_runs ar WHERE ar.agent_id = a.id) as run_count,
                      (SELECT MAX(ar.started_at) FROM agent_runs ar WHERE ar.agent_id = a.id) as last_run
               FROM agents a
               LEFT JOIN projects p ON p.id = a.project_id
               WHERE a.status = ?
               ORDER BY a.name""",
            (status,),
        ).fetchall()

    if not rows:
        return [TextContent(type="text", text=f"No agents with status '{status}'.")]

    lines = [f"Found {len(rows)} agent(s):\n"]
    for r in rows:
        project = r["project_name"] or "unbound"
        desc = (r["description"] or "")[:80]
        lines.append(
            f"  [{r['id']}] {r['name']} ({r['model']}) — project: {project}, "
            f"runs: {r['run_count']}, last: {(r['last_run'] or 'never')[:16]}"
        )
        if r["spec_path"]:
            lines.append(f"      spec_path: {r['spec_path']}")
        if "workstation" in r.keys() and r["workstation"]:
            lines.append(f"      workstation: {r['workstation']}")
        if desc:
            lines.append(f"      {desc}")
    return [TextContent(type="text", text="\n".join(lines))]

async def handle_agent_create(args):
    """Create a new agent."""
    name = str(args.get("name") or "").strip()
    system_prompt = str(args.get("system_prompt") or "").strip()
    description = str(args.get("description") or "").strip()
    model = args.get("model", "openai/gpt-5.4")
    project_name = str(args.get("project") or "").strip()
    max_turns = args.get("max_turns", 20)
    spec_path = str(args.get("spec_path") or "").strip() or None
    workstation = str(args.get("workstation") or "").strip() or None
    log_query("agent_create", project_name, args)

    if not name:
        return [TextContent(type="text", text="Error: 'name' is required.")]
    _available_models, model_error = _validate_agent_model_name(model)
    if model_error:
        return [TextContent(type="text", text=model_error)]

    with db_connection() as conn:
        _ensure_agent_workstation_column(conn)
        # Check name uniqueness
        existing = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
        if existing:
            return [TextContent(type="text", text=f"Error: agent '{name}' already exists (ID {existing['id']}).")]

        # Resolve project if provided
        project_id = None
        if project_name:
            project = get_project_by_name(conn, project_name)
            if not project:
                return [TextContent(type="text", text=f"Error: project '{project_name}' not found.")]
            project_id = project["id"]

        if spec_path and not system_prompt:
            try:
                _project, _normalized_spec_path, spec = _load_agent_spec(
                    conn,
                    {"project_id": project_id, "spec_path": spec_path},
                )
            except Exception as exc:
                return [TextContent(type="text", text=f"Error loading agent spec: {exc}")]
            system_prompt = str(spec.get("task") or "").strip()
            workstation = workstation or str(spec.get("workstation") or "").strip() or None

        workstation, workstation_error = _validate_workstation(conn, workstation)
        if workstation_error:
            return [TextContent(type="text", text=workstation_error)]

        if not system_prompt:
            return [TextContent(type="text", text="Error: 'system_prompt' is required unless 'spec_path' provides a YAML task.")]

        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO agents (name, description, system_prompt, model,
               project_id, max_turns, spec_path, workstation, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, description, system_prompt, model, project_id, max_turns, spec_path, workstation, now),
        )
        conn.commit()
        agent_id = cursor.lastrowid

    return [TextContent(
        type="text",
        text=f"Agent '{name}' created (ID {agent_id}, model: {model}, max_turns: {max_turns}, workstation: {workstation or 'none'}).",
    )]

async def handle_agent_update(args):
    """Update an existing agent."""
    identifier = args.get("agent", "")
    log_query("agent_update", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    with db_connection() as conn:
        _ensure_agent_workstation_column(conn)
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]

        updates = []
        params = []

        if "name" in args and args["name"]:
            updates.append("name = ?")
            params.append(args["name"].strip())
        if "system_prompt" in args and args["system_prompt"]:
            updates.append("system_prompt = ?")
            params.append(args["system_prompt"].strip())
        if "description" in args:
            updates.append("description = ?")
            params.append(args["description"].strip())
        if "model" in args:
            _available_models, model_error = _validate_agent_model_name(args["model"])
            if model_error:
                return [TextContent(type="text", text=model_error)]
            updates.append("model = ?")
            params.append(args["model"])
        if "project" in args:
            if args["project"] == "":
                updates.append("project_id = ?")
                params.append(None)
            else:
                project = get_project_by_name(conn, args["project"])
                if not project:
                    return [TextContent(type="text", text=f"Project '{args['project']}' not found.")]
                updates.append("project_id = ?")
                params.append(project["id"])
        if "max_turns" in args:
            updates.append("max_turns = ?")
            params.append(int(args["max_turns"]))
        if "spec_path" in args:
            updates.append("spec_path = ?")
            params.append(args["spec_path"].strip() if args["spec_path"] else None)
        if "workstation" in args:
            if args["workstation"] == "":
                updates.append("workstation = ?")
                params.append(None)
            else:
                workstation, workstation_error = _validate_workstation(conn, args["workstation"])
                if workstation_error:
                    return [TextContent(type="text", text=workstation_error)]
                updates.append("workstation = ?")
                params.append(workstation)

        if not updates:
            return [TextContent(type="text", text="Nothing to update — pass at least one field to change.")]

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(agent["id"])

        conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    return [TextContent(
        type="text",
        text=f"Agent '{agent['name']}' (ID {agent['id']}) updated: {', '.join(u.split(' =')[0] for u in updates[:-1])}.",
    )]

async def handle_get_agent_spec(args):
    identifier = args.get("name", "").strip()

    if not identifier:
        return [TextContent(type="text", text="Error: 'name' is required.")]

    with db_connection() as conn:
        _ensure_agent_workstation_column(conn)
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]

        try:
            project, normalized_spec_path, spec = _load_agent_spec(conn, agent)
        except Exception as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

    payload = {
        "name": agent["name"],
        "project": project["name"],
        "spec_path": normalized_spec_path,
        "spec": spec,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]

async def handle_agent_delete(args):
    """Soft-delete an agent."""
    identifier = args.get("agent", "")
    log_query("agent_delete", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    with db_connection() as conn:
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]

        conn.execute("UPDATE agents SET status = 'deleted' WHERE id = ?", (agent["id"],))
        conn.commit()

    return [TextContent(type="text", text=f"Agent '{agent['name']}' (ID {agent['id']}) deleted.")]

async def handle_agent_run(args):
    """Run an agent via Custodian's embedded agent runtime, or OpenCode otherwise."""
    identifier = args.get("agent", "")
    user_prompt = args.get("prompt", "")
    input_payload = args.get("input", {})
    log_query("agent_run", None, args)

    if not identifier:
        return [TextContent(type="text", text="Error: 'agent' (name or ID) is required.")]

    is_yaml_agent = False

    with db_connection() as conn:
        agent = _resolve_agent(conn, identifier)
        if not agent:
            return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]
        agent = dict(agent)
        is_yaml_agent = bool(agent.get("spec_path"))

        if is_yaml_agent and not isinstance(input_payload, dict):
            return [TextContent(type="text", text="Error: 'input' must be an object for YAML-backed agents.")]

        # Resolve project working directory
        cwd = CUSTODIAN_ROOT  # fallback to custodian dir
        if agent.get("project_id"):
            proj = conn.execute(
                "SELECT path FROM projects WHERE id = ?", (agent["project_id"],)
            ).fetchone()
            if proj:
                cwd = _to_native_path(proj["path"])

        # Create run record
        run_input = json.dumps(input_payload) if is_yaml_agent else (user_prompt or agent.get("description", ""))
        cursor = conn.execute(
            """INSERT INTO agent_runs (agent_id, input, triggered_by, status)
               VALUES (?, ?, 'mcp', 'running')""",
            (agent["id"], run_input),
        )
        conn.commit()
        run_id = cursor.lastrowid

    prompt_text = user_prompt or agent.get("description") or f"Execute your purpose as {agent['name']}"
    fallback_warning = None

    if agent.get("workstation"):
        try:
            from custodian.services.workstations import dispatch_agent

            dispatch_task = user_prompt or (json.dumps(input_payload, sort_keys=True) if input_payload else prompt_text)
            result = dispatch_agent(agent["name"], dispatch_task, agent_run_id=run_id)
            with db_connection() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='completed', output=?,
                       finished_at=datetime('now') WHERE id=?""",
                    (json.dumps(result), run_id),
                )
                conn.commit()
            return [TextContent(
                type="text",
                text=f"Agent '{agent['name']}' completed via workstation '{agent['workstation']}' (run #{run_id}).\n\n{json.dumps(result, indent=2)}",
            )]
        except Exception as e:
            with db_connection() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='failed', error=?,
                       finished_at=datetime('now') WHERE id=?""",
                    (str(e), run_id),
                )
                conn.commit()
            return [TextContent(type="text", text=f"Agent '{agent['name']}' failed via workstation (run #{run_id}).\nError: {e}")]

    if is_yaml_agent:
        try:
            with db_connection() as conn:
                _project, normalized_spec_path, spec_dict = _load_agent_spec(conn, agent)
            spec = load_spec(spec_dict)
            if not isinstance(spec, LlmAgentSpec):
                raise ValueError(f"spec '{normalized_spec_path}' is type '{spec.type}', expected 'llm_agent'")

            result = await execute_agent(spec=spec, input_data=input_payload, model_override=None)
            output_text = json.dumps(result.output, indent=2)
            tokens_used = None
            if result.tokens_input is not None or result.tokens_output is not None:
                tokens_used = (result.tokens_input or 0) + (result.tokens_output or 0)

            with db_connection() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='completed', output=?,
                       tokens_used=?, finished_at=datetime('now') WHERE id=?""",
                    (json.dumps(result.output), tokens_used, run_id),
                )
                conn.commit()

            meta = [f"run #{run_id}", f"spec: {normalized_spec_path}"]
            if tokens_used:
                meta.append(f"{tokens_used} tokens")
            if result.cost_usd is not None:
                meta.append(f"${result.cost_usd:.4f}")

            return [TextContent(
                type="text",
                text=f"Agent '{agent['name']}' completed via Custodian agent executor ({', '.join(meta)}).\n\n{output_text}",
            )]
        except Exception as e:
            error_text = str(e)
            with db_connection() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='failed', error=?,
                       finished_at=datetime('now') WHERE id=?""",
                    (error_text, run_id),
                )
                conn.commit()
            return [TextContent(
                type="text",
                text=f"Agent '{agent['name']}' failed via Custodian agent executor (run #{run_id}).\nError: {error_text}",
            )]

    try:
        result = run_opencode(
            prompt=prompt_text,
            model=agent["model"],
            system_prompt=agent["system_prompt"],
            project_dir=cwd,
            max_turns=agent.get("max_turns"),
            timeout=600,
        )
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='completed', output=?,
                   tokens_used=?, finished_at=datetime('now') WHERE id=?""",
                (result.text, result.tokens_used, run_id),
            )
            conn.commit()

        meta = [f"run #{run_id}"]
        if result.tokens_used:
            meta.append(f"{result.tokens_used} tokens")
        if result.cost_usd is not None:
            meta.append(f"${result.cost_usd:.4f}")

        return [TextContent(
            type="text",
            text=f"{fallback_warning + chr(10) if fallback_warning else ''}Agent '{agent['name']}' completed ({', '.join(meta)}).\n\n{result.text}",
        )]

    except OpenCodeRunnerError as e:
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', output=?, error=?,
                   tokens_used=?, finished_at=datetime('now') WHERE id=?""",
                (e.text, e.stderr or str(e), e.tokens_used, run_id),
            )
            conn.commit()
        return [TextContent(
            type="text",
            text=f"Agent '{agent['name']}' failed (run #{run_id}).\nError: {e.stderr or str(e)}\nOutput: {e.text[:2000]}",
        )]
    except Exception as e:
        with db_connection() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', error=?,
                   finished_at=datetime('now') WHERE id=?""",
                (str(e), run_id),
            )
            conn.commit()
        return [TextContent(type="text", text=f"Agent run error: {e}")]

async def handle_agent_runs(args):
    """Get run history for agents."""
    identifier = args.get("agent", "")
    limit = min(args.get("limit", 10), 50)

    with db_connection() as conn:
        agent_id = None
        agent_name = "all agents"
        if identifier:
            agent = _resolve_agent(conn, identifier)
            if not agent:
                return [TextContent(type="text", text=f"Agent '{identifier}' not found.")]
            agent_id = agent["id"]
            agent_name = agent["name"]

        if agent_id:
            rows = conn.execute(
                """SELECT ar.*, a.name as agent_name
                   FROM agent_runs ar
                   JOIN agents a ON a.id = ar.agent_id
                   WHERE ar.agent_id = ?
                   ORDER BY ar.started_at DESC LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ar.*, a.name as agent_name
                   FROM agent_runs ar
                   JOIN agents a ON a.id = ar.agent_id
                   ORDER BY ar.started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    if not rows:
        return [TextContent(type="text", text=f"No runs found for {agent_name}.")]

    lines = [f"Last {len(rows)} run(s) for {agent_name}:\n"]
    for r in rows:
        status_icon = {"completed": "+", "failed": "X", "running": "~"}.get(r["status"], "?")
        tokens = r["tokens_used"] or 0
        output_preview = (r["output"] or "")[:100].replace("\n", " ")
        lines.append(
            f"  [{status_icon}] #{r['id']} {r['agent_name']} — {r['status']} "
            f"({(r['started_at'] or '')[:16]}, {tokens} tokens)"
        )
        if r["error"]:
            lines.append(f"      Error: {r['error'][:100]}")
        elif output_preview:
            lines.append(f"      {output_preview}...")
    return [TextContent(type="text", text="\n".join(lines))]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def agent_list(conn, **params):
    return _unwrap(await handle_agent_list(params))


async def agent_create(conn, **params):
    return _unwrap(await handle_agent_create(params))


async def agent_update(conn, **params):
    return _unwrap(await handle_agent_update(params))


async def get_agent_spec(conn, **params):
    return _unwrap(await handle_get_agent_spec(params))


async def agent_delete(conn, **params):
    return _unwrap(await handle_agent_delete(params))


async def agent_run(conn, **params):
    return _unwrap(await handle_agent_run(params))


async def agent_runs(conn, **params):
    return _unwrap(await handle_agent_runs(params))
