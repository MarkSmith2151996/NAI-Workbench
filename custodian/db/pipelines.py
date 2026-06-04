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

from pathlib import Path
from custodian.pipeline import DEFAULT_OUTPUT_BASE, PipelineError, PipelinePaused, PipelineRun, PipelineSpec
from datetime import timedelta
from datetime import datetime as _datetime

def _pipeline_specs_dir():
    return os.path.join(CUSTODIAN_ROOT, "pipelines")

def _pipeline_output_root():
    return DEFAULT_OUTPUT_BASE

def _pipeline_row_by_ref(conn, pipeline_ref):
    text_ref = str(pipeline_ref or "").strip()
    if not text_ref:
        return None
    if text_ref.isdigit():
        return conn.execute("SELECT * FROM pipelines WHERE id = ?", (int(text_ref),)).fetchone()
    return conn.execute("SELECT * FROM pipelines WHERE name = ?", (text_ref,)).fetchone()

def _pipeline_run_summary(conn, run_row):
    pipeline = conn.execute("SELECT name FROM pipelines WHERE id = ?", (run_row["pipeline_id"],)).fetchone()
    step_rows = conn.execute(
        """
        SELECT step_name, step_type, status, duration_ms, error, iteration_index, output
        FROM pipeline_step_results
        WHERE run_id = ?
        ORDER BY id
        """,
        (run_row["id"],),
    ).fetchall()
    grouped = collections.OrderedDict()
    foreach_counts = {}
    for row in step_rows:
        key = (row["step_name"], row["iteration_index"])
        if row["iteration_index"] is None:
            if row["step_name"] not in grouped:
                item = {
                    "name": row["step_name"],
                    "type": row["step_type"],
                    "status": row["status"],
                    "duration_ms": row["duration_ms"],
                    "error": row["error"],
                }
                if row["step_type"] in {"foreach", "watcher"} and row["output"]:
                    payload = json.loads(row["output"])
                    results = payload.get("results", []) if isinstance(payload, dict) else payload
                    item.update({
                        "total": len(results),
                        "completed": len([r for r in results if r.get("status") == "completed"]),
                        "failed": len([r for r in results if r.get("status") == "failed"]),
                    })
                grouped[row["step_name"]] = item
        else:
            foreach_counts.setdefault(row["step_name"], []).append(dict(row))
    return {
        "run_id": run_row["id"],
        "pipeline": pipeline["name"] if pipeline else None,
        "run_name": run_row["run_name"],
        "status": run_row["status"],
        "current_step": run_row["current_step"],
        "stats": json.loads(run_row["stats"]) if run_row["stats"] else None,
        "output_dir": run_row["output_dir"],
        "error": run_row["error"],
        "steps": list(grouped.values()),
    }

async def handle_create_pipeline(args):
    name = str(args.get("name") or "").strip()
    spec_text = str(args.get("spec") or "")
    description_override = str(args.get("description") or "").strip() or None
    log_query("create_pipeline", None, {"name": name})

    if not name or not spec_text.strip():
        return [TextContent(type="text", text="Error: 'name' and 'spec' are required.")]

    try:
        spec = PipelineSpec.from_yaml(spec_text)
    except PipelineError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    if spec.name != name:
        return [TextContent(type="text", text=f"Error: spec name '{spec.name}' does not match requested name '{name}'.")]

    specs_dir = _pipeline_specs_dir()
    os.makedirs(specs_dir, exist_ok=True)
    spec_path = os.path.join(specs_dir, f"{name}.yaml")
    with open(spec_path, "w", encoding="utf-8") as handle:
        handle.write(spec_text)

    with db_connection() as conn:
        existing = conn.execute("SELECT id, created_at FROM pipelines WHERE name = ?", (name,)).fetchone()
        conn.execute(
            """
            INSERT OR REPLACE INTO pipelines (
                id, name, description, version, spec, input_schema, trigger_type, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
            """,
            (
                existing["id"] if existing else None,
                name,
                description_override or spec.description,
                spec.version,
                spec_text,
                json.dumps(spec.input_schema),
                spec.trigger,
                existing["created_at"] if existing else None,
            ),
        )
        row = conn.execute("SELECT * FROM pipelines WHERE name = ?", (name,)).fetchone()
        conn.commit()

    result = {
        "id": row["id"],
        "name": row["name"],
        "steps_count": len(spec.steps),
        "step_names": [step.name for step in spec.steps],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_invoke_pipeline(args):
    pipeline_ref = args.get("pipeline")
    input_data = args.get("input")
    log_query("invoke_pipeline", None, {"pipeline": pipeline_ref})

    if not isinstance(input_data, dict):
        return [TextContent(type="text", text="Error: 'input' must be an object.")]

    with db_connection() as conn:
        pipeline_row = _pipeline_row_by_ref(conn, pipeline_ref)
        if not pipeline_row:
            return [TextContent(type="text", text=f"Error: pipeline '{pipeline_ref}' not found.")]

    try:
        spec = PipelineSpec.from_yaml(pipeline_row["spec"])
    except PipelineError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    errors = spec.validate_input(input_data)
    if errors:
        return [TextContent(type="text", text=json.dumps({"status": "failed", "errors": errors}, indent=2))]

    run_name = "run_" + datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = os.path.join(_pipeline_output_root(), spec.name, run_name)
    os.makedirs(output_dir, exist_ok=True)

    with db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pipeline_runs (pipeline_id, run_name, input, output_dir, status, current_step)
            VALUES (?, ?, ?, ?, 'running', NULL)
            """,
            (pipeline_row["id"], run_name, json.dumps(input_data), output_dir),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()

    runner = PipelineRun(
        spec,
        input_data,
        DB_PATH,
        _pipeline_output_root(),
        pipeline_id=pipeline_row["id"],
        run_id=run_id,
        run_name=run_name,
        output_dir=output_dir,
    )
    try:
        result = await runner.execute()
    except Exception:
        with db_connection() as conn:
            run_row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
            result = _pipeline_run_summary(conn, run_row)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_pipeline_run(args):
    run_id = args.get("run_id")
    pipeline_name = str(args.get("pipeline") or "").strip()
    run_name = str(args.get("run_name") or "").strip()

    with db_connection() as conn:
        run_row = None
        if run_id is not None:
            run_row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (int(run_id),)).fetchone()
        elif pipeline_name and run_name:
            run_row = conn.execute(
                """
                SELECT pr.*
                FROM pipeline_runs pr
                JOIN pipelines p ON p.id = pr.pipeline_id
                WHERE p.name = ? AND pr.run_name = ?
                """,
                (pipeline_name, run_name),
            ).fetchone()
        else:
            return [TextContent(type="text", text="Error: provide run_id or pipeline + run_name.")]
        if not run_row:
            return [TextContent(type="text", text="Error: pipeline run not found.")]
        result = _pipeline_run_summary(conn, run_row)
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_resume_pipeline_run(args):
    run_id = args.get("run_id")
    from_step = str(args.get("from_step") or "").strip() or None
    gate_input = args.get("input", {})
    log_query("resume_pipeline_run", None, {"run_id": run_id, "from_step": from_step, "input": gate_input})

    if run_id is None:
        return [TextContent(type="text", text="Error: 'run_id' is required.")]
    if gate_input is None:
        gate_input = {}
    if not isinstance(gate_input, dict):
        return [TextContent(type="text", text="Error: 'input' must be an object when provided.")]

    with db_connection() as conn:
        run_row = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (int(run_id),)).fetchone()
        if not run_row:
            return [TextContent(type="text", text="Error: pipeline run not found.")]
        pipeline_row = conn.execute("SELECT * FROM pipelines WHERE id = ?", (run_row["pipeline_id"],)).fetchone()
        if not pipeline_row:
            return [TextContent(type="text", text="Error: pipeline for run not found.")]

        if run_row["status"] == "paused":
            current_step = run_row["current_step"]
            waiting_row = conn.execute(
                """
                SELECT * FROM pipeline_step_results
                WHERE run_id = ? AND step_name = ? AND status = 'waiting'
                ORDER BY id DESC LIMIT 1
                """,
                (int(run_id), current_step),
            ).fetchone()
            if not waiting_row:
                return [TextContent(type="text", text=f"Error: paused run is missing waiting gate data for '{current_step}'.")]

            awaiting = json.loads(waiting_row["input"]) if waiting_row["input"] else {}
            for field, schema in awaiting.items():
                if isinstance(schema, dict) and schema.get("required") and field not in gate_input:
                    return [TextContent(type="text", text=f"Error: human gate '{current_step}' requires field '{field}'.")]

            output_file = waiting_row["output_file"]
            if output_file:
                Path(output_file).write_text(json.dumps(gate_input, indent=2), encoding="utf-8")

            conn.execute(
                """
                UPDATE pipeline_step_results
                SET status = 'completed', output = ?, finished_at = datetime('now'), error = NULL
                WHERE id = ?
                """,
                (json.dumps(gate_input), waiting_row["id"]),
            )
            conn.execute(
                "UPDATE pipeline_runs SET status = 'running', error = NULL WHERE id = ?",
                (int(run_id),),
            )
            conn.commit()
            if from_step is None:
                from_step = current_step

    spec = PipelineSpec.from_yaml(pipeline_row["spec"])
    runner = PipelineRun(
        spec,
        json.loads(run_row["input"]),
        DB_PATH,
        _pipeline_output_root(),
        pipeline_id=pipeline_row["id"],
        run_id=run_row["id"],
        run_name=run_row["run_name"],
        output_dir=run_row["output_dir"],
    )
    try:
        result = await runner.resume(from_step=from_step)
    except Exception:
        with db_connection() as conn:
            latest = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (int(run_id),)).fetchone()
            result = _pipeline_run_summary(conn, latest)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_list_pipelines(args):
    status = str(args.get("status") or "").strip()

    query = """
        SELECT p.*, (
            SELECT pr.status FROM pipeline_runs pr
            WHERE pr.pipeline_id = p.id
            ORDER BY pr.started_at DESC, pr.id DESC LIMIT 1
        ) AS last_run_status
        FROM pipelines p
    """
    params = []
    if status:
        query += " WHERE p.status = ?"
        params.append(status)
    query += " ORDER BY p.name"

    with db_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        try:
            spec = PipelineSpec.from_yaml(row["spec"])
            steps_count = len(spec.steps)
        except Exception:
            steps_count = None
        result.append({
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "status": row["status"],
            "steps_count": steps_count,
            "last_run_status": row["last_run_status"],
        })
    return [TextContent(type="text", text=json.dumps(result, indent=2))]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def create_pipeline(conn, **params):
    return _unwrap(await handle_create_pipeline(params))


async def invoke_pipeline(conn, **params):
    return _unwrap(await handle_invoke_pipeline(params))


async def get_pipeline_run(conn, **params):
    return _unwrap(await handle_get_pipeline_run(params))


async def resume_pipeline_run(conn, **params):
    return _unwrap(await handle_resume_pipeline_run(params))


async def list_pipelines(conn, **params):
    return _unwrap(await handle_list_pipelines(params))
