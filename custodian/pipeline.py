from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


DEFAULT_BRIDGE_URL = "http://localhost:9099/call-tool"
DEFAULT_OUTPUT_BASE = "/mnt/c/Users/Big A/custodian-shared/pipelines"
_REF_TOKEN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\d+))*")
_PURE_REF_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\d+))*$")


class PipelineError(Exception):
    """Raised when pipeline spec parsing or execution fails."""


class PipelinePaused(Exception):
    """Raised when a human gate pauses pipeline execution."""

    def __init__(
        self,
        *,
        run_id: int | None,
        step_name: str,
        description: str,
        awaiting: dict[str, Any],
        iteration_index: int | None = None,
        iteration_key: str | None = None,
    ) -> None:
        super().__init__(description)
        self.run_id = run_id
        self.step_name = step_name
        self.description = description
        self.awaiting = awaiting
        self.iteration_index = iteration_index
        self.iteration_key = iteration_key


@dataclass
class StepSpec:
    name: str
    type: str
    agent: str | None = None
    project: str | None = None
    tool: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    items: Any = None
    task_template: str | None = None
    output: str | None = None
    run_if: Any = None
    over: Any = None
    as_var: str | None = None
    description: Any = None
    awaiting: dict[str, Any] = field(default_factory=dict)
    steps: list["StepSpec"] = field(default_factory=list)
    parallel: int | None = None
    watch: str | None = None
    poll_tool: str | None = None
    poll_project: str | None = None
    poll_input: dict[str, Any] = field(default_factory=dict)
    poll_interval: int = 10
    item_key: str = "id"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepSpec":
        if not isinstance(data, dict):
            raise PipelineError(f"step must be an object, got {type(data).__name__}")
        name = str(data.get("name") or "").strip()
        step_type = str(data.get("type") or "").strip()
        awaiting: dict[str, Any] = {}
        if not name:
            raise PipelineError("step missing required field: name")
        if step_type not in {"tool", "foreach", "agent", "human_gate", "watcher"}:
            raise PipelineError(f"step '{name}' has unsupported type '{step_type}'")
        if step_type == "tool":
            if not str(data.get("project") or "").strip():
                raise PipelineError(f"tool step '{name}' missing required field: project")
            if not str(data.get("tool") or "").strip():
                raise PipelineError(f"tool step '{name}' missing required field: tool")
            if not str(data.get("output") or "").strip():
                raise PipelineError(f"tool step '{name}' missing required field: output")
        if step_type == "foreach":
            is_agent_shortcut = bool(str(data.get("agent") or "").strip())
            if is_agent_shortcut:
                if data.get("items") is None and not data.get("over"):
                    raise PipelineError(f"foreach agent step '{name}' missing required field: items or over")
                if not str(data.get("task_template") or "").strip():
                    raise PipelineError(f"foreach agent step '{name}' missing required field: task_template")
                if not str(data.get("output") or "").strip():
                    raise PipelineError(f"foreach agent step '{name}' missing required field: output")
            else:
                if not data.get("over"):
                    raise PipelineError(f"foreach step '{name}' missing required field: over")
                as_var = str(data.get("as") or "").strip()
                if not as_var:
                    raise PipelineError(f"foreach step '{name}' missing required field: as")
                nested = data.get("steps")
                if not isinstance(nested, list) or not nested:
                    raise PipelineError(f"foreach step '{name}' requires a non-empty steps list")
        if step_type == "agent":
            if not str(data.get("agent") or "").strip():
                raise PipelineError(f"agent step '{name}' missing required field: agent")
            if not str(data.get("output") or "").strip():
                raise PipelineError(f"agent step '{name}' missing required field: output")
        if step_type == "human_gate":
            if not data.get("description"):
                raise PipelineError(f"human_gate step '{name}' missing required field: description")
            awaiting = data.get("awaiting") or {}
            if not isinstance(awaiting, dict) or not awaiting:
                raise PipelineError(f"human_gate step '{name}' requires a non-empty awaiting object")
        parallel = data.get("parallel")
        if parallel is not None:
            parallel = int(parallel)
            if parallel < 1:
                raise PipelineError(f"step '{name}' parallel must be >= 1")
        poll_interval = int(data.get("poll_interval", 10) or 0)
        if poll_interval < 0:
            raise PipelineError(f"watcher step '{name}' poll_interval must be >= 0")
        if step_type == "watcher":
            if not str(data.get("watch") or "").strip():
                raise PipelineError(f"watcher step '{name}' missing required field: watch")
            if not str(data.get("poll_tool") or "").strip():
                raise PipelineError(f"watcher step '{name}' missing required field: poll_tool")
            nested = data.get("steps")
            if not isinstance(nested, list) or not nested:
                raise PipelineError(f"watcher step '{name}' requires a non-empty steps list")
        nested_steps = [cls.from_dict(step) for step in data.get("steps", []) or []]
        return cls(
            name=name,
            type=step_type,
            agent=str(data.get("agent") or "").strip() or None,
            project=str(data.get("project") or "").strip() or None,
            tool=str(data.get("tool") or "").strip() or None,
            input=data.get("input") or {},
            items=data.get("items"),
            task_template=str(data.get("task_template") or "").strip() or None,
            output=str(data.get("output") or "").strip() or None,
            run_if=data.get("run_if"),
            over=data.get("over"),
            as_var=str(data.get("as") or "").strip() or None,
            description=data.get("description"),
            awaiting=awaiting if step_type == "human_gate" else {},
            steps=nested_steps,
            parallel=parallel,
            watch=str(data.get("watch") or "").strip() or None,
            poll_tool=str(data.get("poll_tool") or "").strip() or None,
            poll_project=str(data.get("poll_project") or "").strip() or None,
            poll_input=data.get("poll_input") or {},
            poll_interval=poll_interval,
            item_key=str(data.get("item_key") or "id").strip() or "id",
        )


@dataclass
class PipelineSpec:
    name: str
    version: int
    description: str
    trigger: str
    input_schema: dict[str, Any]
    steps: list[StepSpec]

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "PipelineSpec":
        try:
            data = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            raise PipelineError(f"invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise PipelineError("pipeline spec must be a YAML object")
        name = str(data.get("name") or "").strip()
        if not name:
            raise PipelineError("pipeline missing required field: name")
        version = data.get("version", 1)
        if not isinstance(version, int):
            raise PipelineError("pipeline version must be an integer")
        trigger = str(data.get("trigger") or "manual").strip() or "manual"
        if trigger != "manual":
            raise PipelineError(f"unsupported trigger '{trigger}' in v1; only 'manual' is allowed")
        input_schema = data.get("input_schema") or {}
        if not isinstance(input_schema, dict):
            raise PipelineError("input_schema must be an object")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PipelineError("pipeline must define a non-empty steps list")
        steps = [StepSpec.from_dict(step) for step in raw_steps]
        _ensure_unique_step_names(steps)
        return cls(
            name=name,
            version=version,
            description=str(data.get("description") or "").strip(),
            trigger=trigger,
            input_schema=input_schema,
            steps=steps,
        )

    def validate_input(self, input_data: dict[str, Any]) -> list[str]:
        if not isinstance(input_data, dict):
            return [f"pipeline input must be an object, got {type(input_data).__name__}"]
        errors: list[str] = []
        for field_name, field_spec in self.input_schema.items():
            if not isinstance(field_spec, dict):
                continue
            value = input_data.get(field_name)
            field_errors = _validate_schema_value(value, field_spec, field_name)
            errors.extend(field_errors)
        return errors


class RefResolver:
    """Resolves $ref expressions against nested execution scopes."""

    def __init__(self, scopes: list[dict[str, Any]] | None = None) -> None:
        self._scopes = scopes[:] if scopes else [{}]

    def set(self, key: str, value: Any) -> None:
        self._scopes[-1][key] = value

    def child(self) -> "RefResolver":
        return RefResolver(self._scopes + [{}])

    def snapshot(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for scope in self._scopes:
            merged.update(scope)
        return merged

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            if _PURE_REF_RE.match(value):
                return self._resolve_ref(value)
            if "$" in value:
                return self._resolve_string(value)
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    def _resolve_string(self, value: str) -> Any:
        if any(token in value for token in (" == ", " != ", " <= ", " >= ", " < ", " > ", " and ", " or ", " not ")):
            return self._evaluate_expression(value)
        return _REF_TOKEN_RE.sub(lambda match: str(self._resolve_ref(match.group(0))), value)

    def _evaluate_expression(self, expression: str) -> Any:
        rewritten = _REF_TOKEN_RE.sub(lambda match: repr(self._resolve_ref(match.group(0))), expression)
        try:
            return eval(rewritten, {"__builtins__": {}}, {})
        except Exception as exc:
            raise PipelineError(f"failed to evaluate expression '{expression}': {exc}") from exc

    def _resolve_ref(self, ref: str) -> Any:
        path = ref[1:]
        if not path:
            raise PipelineError("$ref resolution failed: '$' is not a valid reference")
        parts = path.split(".")
        root_name = parts[0]
        root_value = self._lookup_root(root_name)
        try:
            return _traverse_path(root_value, parts[1:], ref, root_name)
        except PipelineError:
            raise

    def _lookup_root(self, root_name: str) -> Any:
        for scope in reversed(self._scopes):
            if root_name in scope:
                return scope[root_name]
        raise PipelineError(f"$ref resolution failed: '{'$' + root_name}' — root '{root_name}' not found in context")


class PipelineRun:
    """Runs a deterministic tool/foreach pipeline and persists results."""

    def __init__(
        self,
        spec: PipelineSpec,
        input_data: dict[str, Any],
        db_path: str,
        output_base: str = DEFAULT_OUTPUT_BASE,
        *,
        pipeline_id: int | None = None,
        run_id: int | None = None,
        run_name: str | None = None,
        output_dir: str | None = None,
        bridge_url: str = DEFAULT_BRIDGE_URL,
    ) -> None:
        self.spec = spec
        self.input_data = input_data
        self.db_path = db_path
        self.pipeline_id = pipeline_id
        self.run_id = run_id
        self.run_name = run_name or _default_run_name()
        self.output_base = Path(output_base)
        self.output_dir = Path(output_dir) if output_dir else self.output_base / spec.name / self.run_name
        self.bridge_url = bridge_url
        self.context = RefResolver([{"input": input_data}])
        self._resumed_step_name: str | None = None
        self._resume_iteration_state: dict[str, dict[int, dict[str, Any]]] = {}
        self._steps_completed = 0
        self._steps_failed = 0
        self._steps_total = _count_steps(spec.steps)
        self._started_monotonic: float | None = None
        self._state_lock = threading.RLock()

    async def execute(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._started_monotonic = time.monotonic()
        self._write_meta(status="running", current_step=None)
        return await self._run_steps(self.spec.steps)

    async def resume(self, from_step: str | None = None) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._started_monotonic = time.monotonic()
        self._rebuild_context_from_disk()
        target = from_step or self._failed_step_name()
        self._resumed_step_name = target
        self._load_resume_iteration_state()
        self._write_meta(status="running", current_step=target)
        return await self._run_steps(self.spec.steps, from_step=target)

    async def _run_steps(self, steps: list[StepSpec], from_step: str | None = None) -> dict[str, Any]:
        skipping = from_step is not None
        consumed_watchers: set[str] = set()
        try:
            for index, step in enumerate(steps):
                if step.name in consumed_watchers:
                    continue
                if skipping:
                    if step.name != from_step:
                        continue
                    skipping = False
                if step.type == "watcher":
                    raise PipelineError(f"watcher step '{step.name}' must watch a top-level step that also runs in this pipeline")
                self._set_run_state(current_step=step.name, status="running")
                watcher_steps = [
                    candidate
                    for candidate in steps[index + 1 :]
                    if candidate.type == "watcher" and candidate.watch == step.name and candidate.name not in consumed_watchers
                ]
                if watcher_steps:
                    watched_started = asyncio.Event()
                    watched_done = asyncio.Event()

                    async def run_watched() -> Any:
                        watched_started.set()
                        try:
                            return await self.execute_step(step, self.context, self.output_dir)
                        finally:
                            watched_done.set()

                    watched_task = asyncio.create_task(run_watched())
                    watcher_tasks = [
                        asyncio.create_task(
                            self.execute_watcher_step(
                                watcher_step,
                                self.context,
                                self.output_dir,
                                watched_started=watched_started,
                                watched_done=watched_done,
                            )
                        )
                        for watcher_step in watcher_steps
                    ]
                    results = await asyncio.gather(watched_task, *watcher_tasks, return_exceptions=True)
                    consumed_watchers.update(watcher.name for watcher in watcher_steps)
                    for result in results:
                        if isinstance(result, Exception):
                            raise result
                else:
                    await self.execute_step(step, self.context, self.output_dir)
            stats = self._final_stats(status="completed")
            self._set_run_state(status="completed", current_step=None, error=None, stats=stats)
            return {
                "run_id": self.run_id,
                "run_name": self.run_name,
                "status": "completed",
                "stats": stats,
                "output_dir": str(self.output_dir),
            }
        except PipelinePaused as paused:
            stats = self._final_stats(status="paused")
            self._set_run_state(
                status="paused",
                current_step=self._resume_root_step(paused.step_name),
                error=None,
                stats=stats,
            )
            return {
                "run_id": self.run_id,
                "run_name": self.run_name,
                "status": "paused",
                "waiting_for": paused.step_name,
                "description": paused.description,
                "awaiting": paused.awaiting,
                "iteration_index": paused.iteration_index,
                "iteration_key": paused.iteration_key,
                "stats": stats,
                "output_dir": str(self.output_dir),
            }
        except Exception as exc:
            stats = self._final_stats(status="failed")
            self._set_run_state(status="failed", error=str(exc), stats=stats)
            raise

    async def execute_step(
        self,
        step: StepSpec,
        context: RefResolver,
        base_dir: Path,
        *,
        iteration_index: int | None = None,
        iteration_key: str | None = None,
    ) -> Any:
        if step.type == "agent":
            return await self.execute_agent_step(
                step,
                context,
                base_dir,
                iteration_index=iteration_index,
                iteration_key=iteration_key,
            )
        if step.type == "tool":
            return await self.execute_tool_step(
                step,
                context,
                base_dir,
                iteration_index=iteration_index,
                iteration_key=iteration_key,
            )
        if step.type == "foreach":
            return await self.execute_foreach_step(step, context, base_dir)
        if step.type == "human_gate":
            return await self.execute_human_gate_step(
                step,
                context,
                base_dir,
                iteration_index=iteration_index,
                iteration_key=iteration_key,
            )
        if step.type == "watcher":
            return await self.execute_watcher_step(step, context, base_dir)
        raise PipelineError(f"unsupported step type: {step.type}")

    async def execute_agent_step(
        self,
        step: StepSpec,
        context: RefResolver,
        base_dir: Path,
        *,
        iteration_index: int | None = None,
        iteration_key: str | None = None,
    ) -> Any:
        if step.run_if is not None and not bool(context.resolve(step.run_if)):
            result = {"status": "skipped", "reason": "run_if evaluated to false"}
            output_path = self._step_output_path(base_dir, step.name)
            output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            self._record_step_result(
                step_name=step.name,
                step_type=step.type,
                status="skipped",
                resolved_input=None,
                output=result,
                output_file=output_path,
                iteration_index=iteration_index,
                iteration_key=iteration_key,
                error=None,
                duration_ms=0,
            )
            context.set(step.name, {step.output: result})
            return result

        resolved_input = context.resolve(step.input)
        started = time.time()
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="running",
            resolved_input=resolved_input,
            iteration_index=iteration_index,
            iteration_key=iteration_key,
            started_at=_utc_now(),
        )

        with self._state_lock:
            with self._db() as conn:
                agent = conn.execute(
                    """
                    SELECT a.*, p.name AS project_name
                    FROM agents a
                    LEFT JOIN projects p ON p.id = a.project_id
                    WHERE a.name = ? AND a.status = 'active'
                    """,
                    (step.agent,),
                ).fetchone()
                if not agent:
                    raise PipelineError(f"Agent '{step.agent}' not found or not active")

                is_workstation_agent = "workstation" in agent.keys() and bool(agent["workstation"])
                tool_defs = [] if is_workstation_agent else self._resolve_agent_tools(conn, agent)
                cursor = conn.execute(
                    """
                    INSERT INTO agent_runs (agent_id, pipeline_id, pipeline_step, input, triggered_by, status)
                    VALUES (?, ?, ?, ?, 'pipeline', 'running')
                    """,
                    (agent["id"], self.pipeline_id, step.name, json.dumps(resolved_input)),
                )
                agent_run_id = int(cursor.lastrowid)
                conn.commit()

        try:
            if is_workstation_agent:
                from custodian.services.workstations import dispatch_agent

                if isinstance(resolved_input, dict):
                    task_text = str(resolved_input.get("task") or resolved_input.get("prompt") or json.dumps(resolved_input, sort_keys=True))
                else:
                    task_text = str(resolved_input)
                agent_output = await asyncio.to_thread(dispatch_agent, step.agent or "", task_text, agent_run_id)
                output_path = self._step_output_path(base_dir, step.name)
                output_path.write_text(json.dumps(agent_output, indent=2), encoding="utf-8")
                duration_ms = int((time.time() - started) * 1000)
                self._finish_step_result(
                    row_id,
                    status="completed",
                    output=agent_output,
                    output_file=output_path,
                    finished_at=_utc_now(),
                    duration_ms=duration_ms,
                    error=None,
                )
                with self._state_lock:
                    with self._db() as conn:
                        conn.execute(
                            """
                            UPDATE agent_runs
                            SET status = 'completed', output = ?, finished_at = datetime('now')
                            WHERE id = ?
                            """,
                            (json.dumps(agent_output), agent_run_id),
                        )
                        conn.commit()
                context.set(step.name, {step.output: agent_output})
                self._steps_completed += 1
                return agent_output

            from custodian.compiler import compile_prompt
            from custodian.executor import run_agent_loop

            compiled = compile_prompt(
                system_prompt=agent["system_prompt"],
                tools=tool_defs,
                input_data=resolved_input,
                db=conn,
            )
            agent_result = await run_agent_loop(
                model=agent["model"] or "openai/gpt-5.4",
                compiled_prompt=compiled,
                max_turns=int(agent["max_turns"] or 20),
                tools=tool_defs,
                bridge_url=self.bridge_url,
            )
            output_path = self._step_output_path(base_dir, step.name)
            output_path.write_text(json.dumps(agent_result.output, indent=2), encoding="utf-8")
            duration_ms = int((time.time() - started) * 1000)
            self._finish_step_result(
                row_id,
                status="completed",
                output=agent_result.output,
                output_file=output_path,
                finished_at=_utc_now(),
                duration_ms=duration_ms,
                error=None,
            )
            with self._state_lock:
                with self._db() as conn:
                    conn.execute(
                        """
                        UPDATE agent_runs
                        SET status = 'completed', output = ?, tokens_used = ?, finished_at = datetime('now')
                        WHERE id = ?
                        """,
                        (json.dumps(agent_result.output), agent_result.tokens_used, agent_run_id),
                    )
                    conn.commit()
            context.set(step.name, {step.output: agent_result.output})
            self._steps_completed += 1
            return agent_result.output
        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            self._steps_failed += 1
            self._finish_step_result(
                row_id,
                status="failed",
                output=None,
                output_file=None,
                finished_at=_utc_now(),
                duration_ms=duration_ms,
                error=str(exc),
            )
            with self._state_lock:
                with self._db() as conn:
                    conn.execute(
                        """
                        UPDATE agent_runs
                        SET status = 'failed', error = ?, finished_at = datetime('now')
                        WHERE id = ?
                        """,
                        (str(exc), agent_run_id),
                    )
                    conn.commit()
            raise

    async def execute_tool_step(self, step: StepSpec, context: RefResolver, base_dir: Path, *, iteration_index: int | None = None, iteration_key: str | None = None) -> Any:
        if step.run_if is not None and not bool(context.resolve(step.run_if)):
            result = {"status": "skipped", "reason": "run_if evaluated to false"}
            output_path = self._step_output_path(base_dir, step.name)
            output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            self._record_step_result(
                step_name=step.name,
                step_type=step.type,
                status="skipped",
                resolved_input=None,
                output=result,
                output_file=output_path,
                iteration_index=iteration_index,
                iteration_key=iteration_key,
                error=None,
                duration_ms=0,
            )
            context.set(step.name, {step.output: result})
            return result

        resolved_input = context.resolve(step.input)
        started = time.time()
        start_iso = _utc_now()
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="running",
            resolved_input=resolved_input,
            iteration_index=iteration_index,
            iteration_key=iteration_key,
            started_at=start_iso,
        )
        try:
            result = await asyncio.to_thread(_call_bridge, self.bridge_url, step.project or "", step.tool or "", resolved_input)
            output_path = self._step_output_path(base_dir, step.name)
            output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            duration_ms = int((time.time() - started) * 1000)
            self._finish_step_result(
                row_id,
                status="completed",
                output=result,
                output_file=output_path,
                finished_at=_utc_now(),
                duration_ms=duration_ms,
                error=None,
            )
            context.set(step.name, {step.output: result})
            self._steps_completed += 1
            return result
        except Exception as exc:
            duration_ms = int((time.time() - started) * 1000)
            self._steps_failed += 1
            self._finish_step_result(
                row_id,
                status="failed",
                output=None,
                output_file=None,
                finished_at=_utc_now(),
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

    async def execute_foreach_step(self, step: StepSpec, context: RefResolver, base_dir: Path) -> dict[str, Any]:
        if step.run_if is not None and not bool(context.resolve(step.run_if)):
            result = {"status": "skipped", "reason": "run_if evaluated to false", "results": []}
            foreach_dir = self._foreach_dir(base_dir, step.name)
            foreach_dir.mkdir(parents=True, exist_ok=True)
            output_path = foreach_dir / "_results.json"
            output_path.write_text(json.dumps([], indent=2), encoding="utf-8")
            self._record_step_result(
                step_name=step.name,
                step_type=step.type,
                status="skipped",
                resolved_input=None,
                output=result,
                output_file=output_path,
                iteration_index=None,
                iteration_key=None,
                error=None,
                duration_ms=0,
            )
            context.set(step.name, {"results": []})
            return result

        if step.agent and self._agent_has_workstation(step.agent):
            return await self._execute_workstation_foreach_step(step, context, base_dir)

        items = context.resolve(step.over)
        if not isinstance(items, list):
            raise PipelineError(f"foreach '{step.name}' over resolved to {type(items).__name__}, expected list")

        foreach_dir = self._foreach_dir(base_dir, step.name)
        foreach_dir.mkdir(parents=True, exist_ok=True)
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="running",
            resolved_input={"over": items},
            iteration_index=None,
            iteration_key=None,
            started_at=_utc_now(),
        )
        started = time.time()
        existing = self._resume_iteration_state.get(step.name, {})
        nested_state = self._load_iteration_step_state(step)
        results: list[dict[str, Any]] = []
        paused_exc: PipelinePaused | None = None

        for index, item in enumerate(items):
            if index in existing and existing[index].get("status") == "completed":
                results.append(existing[index])

        pending_items = [
            (index, item)
            for index, item in enumerate(items)
            if not (index in existing and existing[index].get("status") == "completed")
        ]

        if step.parallel and step.parallel > 1:
            semaphore = asyncio.Semaphore(step.parallel)

            async def run_item(index: int, item: Any) -> dict[str, Any]:
                async with semaphore:
                    return await self._execute_iteration_item(
                        parent_step=step,
                        item=item,
                        index=index,
                        context=context,
                        parent_dir=foreach_dir,
                        item_var=step.as_var or "item",
                        existing_item=existing.get(index),
                        per_item_state=nested_state.get(index, {}),
                    )

            processed = await asyncio.gather(*(run_item(index, item) for index, item in pending_items))
            results.extend(processed)
            for item_result in processed:
                if item_result.get("status") == "paused" and paused_exc is None:
                    paused_exc = item_result.get("_pause")
        else:
            for index, item in pending_items:
                item_result = await self._execute_iteration_item(
                    parent_step=step,
                    item=item,
                    index=index,
                    context=context,
                    parent_dir=foreach_dir,
                    item_var=step.as_var or "item",
                    existing_item=existing.get(index),
                    per_item_state=nested_state.get(index, {}),
                )
                results.append(item_result)
                if item_result.get("status") == "paused" and paused_exc is None:
                    paused_exc = item_result.get("_pause")

        failures = len([entry for entry in results if entry.get("status") == "failed"])
        self._steps_failed += failures

        results.sort(key=lambda entry: entry.get("index", 0))
        for entry in results:
            entry.pop("_pause", None)
        summary = {
            "total": len(items),
            "completed": len([entry for entry in results if entry.get("status") == "completed"]),
            "failed": len([entry for entry in results if entry.get("status") == "failed"]),
            "results": results,
        }
        if any(entry.get("status") == "paused" for entry in results):
            summary["paused"] = len([entry for entry in results if entry.get("status") == "paused"])
        output_path = foreach_dir / "_results.json"
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        if paused_exc is not None:
            self._write_foreach_partial(row_id, foreach_dir, results, started, paused=True)
            raise paused_exc
        self._finish_step_result(
            row_id,
            status="failed" if failures else "completed",
            output=summary,
            output_file=output_path,
            finished_at=_utc_now(),
            duration_ms=int((time.time() - started) * 1000),
            error=f"{failures} iteration(s) failed" if failures else None,
        )
        context.set(step.name, {"results": results})
        self._steps_completed += 1
        return summary


    async def _execute_workstation_foreach_step(self, step: StepSpec, context: RefResolver, base_dir: Path) -> dict[str, Any]:
        raw_items = step.items if step.items is not None else step.over
        items = context.resolve(raw_items)
        if not isinstance(items, list):
            raise PipelineError(f"foreach '{step.name}' items resolved to {type(items).__name__}, expected list")
        foreach_dir = self._foreach_dir(base_dir, step.name)
        foreach_dir.mkdir(parents=True, exist_ok=True)
        tasks = [self._render_task_template(step.task_template or "", item, index) for index, item in enumerate(items)]
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="running",
            resolved_input={"items": items, "tasks": tasks, "agent": step.agent},
            iteration_index=None,
            iteration_key=None,
            started_at=_utc_now(),
        )
        started = time.time()
        try:
            from custodian.services.workstations import dispatch_batch

            batch = await asyncio.to_thread(dispatch_batch, step.agent or "", tasks, step.parallel)
            results: list[dict[str, Any]] = []
            for index, (item, task_text, payload) in enumerate(zip(items, tasks, batch.get("results", []))):
                item_key = _iteration_key(item, index)
                item_dir = foreach_dir / f"{index}_{_safe_slug(item_key)}"
                item_dir.mkdir(parents=True, exist_ok=True)
                item_result = {
                    "index": index,
                    "key": item_key,
                    "item": item,
                    "task": task_text,
                    "status": "completed" if payload.get("ok", True) else "failed",
                    step.output or "result": payload,
                }
                if not payload.get("ok", True):
                    item_result["error"] = payload.get("error")
                (item_dir / f"{step.name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
                results.append(item_result)
            failures = len([entry for entry in results if entry.get("status") == "failed"])
            summary = {
                "total": len(items),
                "completed": len(items) - failures,
                "failed": failures,
                "results": results,
            }
            output_path = foreach_dir / "_results.json"
            output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
            self._finish_step_result(
                row_id,
                status="failed" if failures else "completed",
                output=summary,
                output_file=output_path,
                finished_at=_utc_now(),
                duration_ms=int((time.time() - started) * 1000),
                error=f"{failures} iteration(s) failed" if failures else None,
            )
            context.set(step.name, {"results": results})
            self._steps_completed += 1
            if failures:
                self._steps_failed += failures
            return summary
        except Exception as exc:
            self._steps_failed += 1
            self._finish_step_result(
                row_id,
                status="failed",
                output=None,
                output_file=None,
                finished_at=_utc_now(),
                duration_ms=int((time.time() - started) * 1000),
                error=str(exc),
            )
            raise

    async def execute_watcher_step(
        self,
        step: StepSpec,
        context: RefResolver,
        base_dir: Path,
        *,
        watched_started: asyncio.Event | None = None,
        watched_done: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if watched_started is None or watched_done is None:
            raise PipelineError(f"watcher step '{step.name}' must be scheduled with its watched step")
        if step.run_if is not None and not bool(context.resolve(step.run_if)):
            result = {"status": "skipped", "reason": "run_if evaluated to false", "results": []}
            watcher_dir = self._foreach_dir(base_dir, step.name)
            watcher_dir.mkdir(parents=True, exist_ok=True)
            output_path = watcher_dir / "_results.json"
            output_path.write_text(json.dumps([], indent=2), encoding="utf-8")
            self._record_step_result(
                step_name=step.name,
                step_type=step.type,
                status="skipped",
                resolved_input=None,
                output=result,
                output_file=output_path,
                iteration_index=None,
                iteration_key=None,
                error=None,
                duration_ms=0,
            )
            context.set(step.name, {"results": []})
            return result

        await watched_started.wait()
        watcher_dir = self._foreach_dir(base_dir, step.name)
        watcher_dir.mkdir(parents=True, exist_ok=True)
        resolved_poll_input = context.resolve(step.poll_input)
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="running",
            resolved_input={
                "watch": step.watch,
                "poll_tool": step.poll_tool,
                "poll_project": step.poll_project,
                "poll_input": resolved_poll_input,
            },
            iteration_index=None,
            iteration_key=None,
            started_at=_utc_now(),
        )
        started = time.time()
        existing = self._resume_iteration_state.get(step.name, {})
        nested_state = self._load_iteration_step_state(step)
        results = [dict(entry) for _, entry in sorted(existing.items())]
        dispatched_keys = {str(entry.get("key")) for entry in results if entry.get("key") is not None}
        next_index = (max(existing.keys()) + 1) if existing else 0
        semaphore = asyncio.Semaphore(step.parallel or 1)
        in_flight: set[asyncio.Task[dict[str, Any]]] = set()
        queued_items: list[tuple[int, Any, str]] = []
        paused_exc: PipelinePaused | None = None
        workstation_agent = self._first_workstation_agent(step.steps)
        workstation_name = self._agent_workstation(workstation_agent) if workstation_agent else None

        async def run_item(index: int, item: Any) -> dict[str, Any]:
            async with semaphore:
                return await self._execute_iteration_item(
                    parent_step=step,
                    item=item,
                    index=index,
                    context=context,
                    parent_dir=watcher_dir,
                    item_var="item",
                    existing_item=existing.get(index),
                    per_item_state=nested_state.get(index, {}),
                )

        while True:
            done_now = [task for task in in_flight if task.done()]
            for task in done_now:
                in_flight.remove(task)
                item_result = task.result()
                results = [entry for entry in results if entry.get("index") != item_result.get("index")]
                results.append(item_result)
                if item_result.get("status") == "paused" and paused_exc is None:
                    paused_exc = item_result.get("_pause")

            if paused_exc is None:
                resolved_poll_input = context.resolve(step.poll_input)
                polled = await asyncio.to_thread(
                    _call_bridge,
                    self.bridge_url,
                    step.poll_project or "",
                    step.poll_tool or "",
                    resolved_poll_input,
                )
                polled_items = self._normalize_watcher_items(step, polled)
                new_items = []
                candidates: list[tuple[int, Any, str]] = queued_items
                queued_items = []
                for item in polled_items:
                    dedupe_key = self._watcher_item_key(step, item, next_index)
                    if dedupe_key in dispatched_keys:
                        continue
                    dispatched_keys.add(dedupe_key)
                    candidates.append((next_index, item, dedupe_key))
                    next_index += 1
                available_slots = None
                if workstation_name:
                    from custodian.services.workstations import available_slot_count, note_queue_depth

                    available_slots = max(0, available_slot_count(workstation_name) - len(in_flight))
                for index, item, dedupe_key in candidates:
                    if available_slots is not None and available_slots <= 0:
                        print(
                            f"Item {dedupe_key} queued — no free slots in workstation {workstation_name}, will retry next cycle",
                            file=sys.stderr,
                        )
                        queued_items.append((index, item, dedupe_key))
                        continue
                    if available_slots is not None:
                        available_slots -= 1
                    new_items.append((index, item))
                    in_flight.add(asyncio.create_task(run_item(index, item)))
                if workstation_name:
                    note_queue_depth(workstation_name, len(queued_items))
            else:
                polled_items = []
                new_items = []

            if watched_done.is_set() and not new_items and not in_flight and not queued_items:
                break
            if paused_exc is not None and not in_flight:
                break
            if in_flight:
                done, pending = await asyncio.wait(in_flight, timeout=step.poll_interval or 0, return_when=asyncio.FIRST_COMPLETED)
                if not done and pending and step.poll_interval > 0:
                    continue
            elif step.poll_interval > 0:
                await asyncio.sleep(step.poll_interval)

        results.sort(key=lambda entry: entry.get("index", 0))
        failures = len([entry for entry in results if entry.get("status") == "failed"])
        self._steps_failed += failures
        summary = {
            "total": len(results),
            "completed": len([entry for entry in results if entry.get("status") == "completed"]),
            "failed": failures,
            "results": [{k: v for k, v in entry.items() if k != "_pause"} for entry in results],
        }
        if paused_exc is not None:
            summary["paused"] = len([entry for entry in results if entry.get("status") == "paused"])
        output_path = watcher_dir / "_results.json"
        output_path.write_text(json.dumps(summary["results"], indent=2), encoding="utf-8")
        if paused_exc is not None:
            self._write_foreach_partial(row_id, watcher_dir, summary["results"], started, paused=True)
            raise paused_exc
        self._finish_step_result(
            row_id,
            status="failed" if failures else "completed",
            output=summary,
            output_file=output_path,
            finished_at=_utc_now(),
            duration_ms=int((time.time() - started) * 1000),
            error=f"{failures} iteration(s) failed" if failures else None,
        )
        context.set(step.name, {"results": summary["results"]})
        self._steps_completed += 1
        return summary

    async def _execute_iteration_item(
        self,
        *,
        parent_step: StepSpec,
        item: Any,
        index: int,
        context: RefResolver,
        parent_dir: Path,
        item_var: str,
        existing_item: dict[str, Any] | None,
        per_item_state: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        item_context = context.child()
        item_context.set(item_var, item)
        item_key = self._watcher_item_key(parent_step, item, index) if parent_step.type == "watcher" else _iteration_key(item, index)
        item_dir = parent_dir / f"{index}_{_safe_slug(item_key)}"
        item_dir.mkdir(parents=True, exist_ok=True)
        item_results: dict[str, Any] = dict(existing_item or {"index": index, "key": item_key})
        item_results["status"] = "completed"

        for nested_step in parent_step.steps:
            cached = per_item_state.get(nested_step.name)
            if not cached or cached["status"] not in {"completed", "skipped"}:
                continue
            payload = cached.get("payload")
            if nested_step.type == "human_gate":
                item_context.set(nested_step.name, payload)
            elif nested_step.output:
                item_context.set(nested_step.name, {nested_step.output: payload})
            item_results[nested_step.name] = payload

        try:
            for nested_step in parent_step.steps:
                cached = per_item_state.get(nested_step.name)
                if cached and cached["status"] in {"completed", "skipped"}:
                    continue
                nested_result = await self.execute_step(
                    nested_step,
                    item_context,
                    item_dir,
                    iteration_index=index,
                    iteration_key=item_key,
                )
                item_results[nested_step.name] = nested_result
            return item_results
        except PipelinePaused as paused:
            item_results["status"] = "paused"
            item_results["_pause"] = paused
            return item_results
        except Exception as exc:
            item_results["status"] = "failed"
            item_results["error"] = str(exc)
            return item_results

    async def execute_human_gate_step(
        self,
        step: StepSpec,
        context: RefResolver,
        base_dir: Path,
        *,
        iteration_index: int | None = None,
        iteration_key: str | None = None,
    ) -> Any:
        existing = self._latest_step_row(step.name, iteration_index)
        if existing and existing["status"] in {"completed", "skipped"}:
            payload = self._load_payload(existing)
            if payload is None:
                payload = {}
            context.set(step.name, payload)
            return payload

        description = context.resolve(step.description) if isinstance(step.description, str) else step.description
        awaiting = context.resolve(step.awaiting)
        gate_file = self._step_output_path(base_dir, step.name)
        gate_payload = {
            "status": "waiting",
            "description": description,
            "awaiting": awaiting,
            "paused_at": datetime.now(timezone.utc).isoformat(),
        }
        gate_file.write_text(json.dumps(gate_payload, indent=2), encoding="utf-8")
        row_id = self._insert_step_result(
            step_name=step.name,
            step_type=step.type,
            status="waiting",
            resolved_input=awaiting,
            iteration_index=iteration_index,
            iteration_key=iteration_key,
            started_at=_utc_now(),
        )
        self._update_step_result_partial(
            row_id,
            output=gate_payload,
            output_file=gate_file,
            error=None,
        )
        raise PipelinePaused(
            run_id=self.run_id,
            step_name=step.name,
            description=str(description),
            awaiting=awaiting if isinstance(awaiting, dict) else {},
            iteration_index=iteration_index,
            iteration_key=iteration_key,
        )

    def _rebuild_context_from_disk(self) -> None:
        self.context = RefResolver([{"input": self.input_data}])
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT step_name, step_type, output_file, output, status
                FROM pipeline_step_results
                WHERE run_id = ? AND iteration_index IS NULL
                ORDER BY id
                """,
                (self.run_id,),
            ).fetchall()
        for row in rows:
            if row["status"] not in {"completed", "skipped", "failed"}:
                continue
            payload = None
            if row["output_file"] and os.path.exists(row["output_file"]):
                payload = json.loads(Path(row["output_file"]).read_text(encoding="utf-8"))
            elif row["output"]:
                payload = json.loads(row["output"])
            if payload is None:
                continue
            if row["step_type"] in {"foreach", "watcher"}:
                self.context.set(row["step_name"], {"results": payload if isinstance(payload, list) else payload.get("results", [])})
            elif row["step_type"] == "human_gate":
                self.context.set(row["step_name"], payload)
            else:
                step_spec = self._find_step(self.spec.steps, row["step_name"])
                if step_spec and step_spec.output:
                    self.context.set(row["step_name"], {step_spec.output: payload})

    def _load_resume_iteration_state(self) -> None:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT step_name, output, status
                FROM pipeline_step_results
                WHERE run_id = ? AND step_type IN ('foreach', 'watcher') AND iteration_index IS NULL
                ORDER BY id
                """,
                (self.run_id,),
            ).fetchall()
        state: dict[str, dict[int, dict[str, Any]]] = {}
        for row in rows:
            if not row["output"]:
                continue
            payload = json.loads(row["output"])
            results = payload.get("results", []) if isinstance(payload, dict) else payload
            mapping: dict[int, dict[str, Any]] = {}
            for item in results:
                if isinstance(item, dict) and "index" in item:
                    mapping[int(item["index"])] = item
            state[row["step_name"]] = mapping
        self._resume_iteration_state = state

    def _failed_step_name(self) -> str | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT current_step FROM pipeline_runs WHERE id = ?",
                (self.run_id,),
            ).fetchone()
        return row["current_step"] if row else None

    def _find_step(self, steps: list[StepSpec], name: str) -> StepSpec | None:
        for step in steps:
            if step.name == name:
                return step
            nested = self._find_step(step.steps, name)
            if nested:
                return nested
        return None

    def _resume_root_step(self, name: str) -> str:
        for step in self.spec.steps:
            if step.name == name or self._find_step(step.steps, name):
                return step.name
        return name

    def _foreach_dir(self, base_dir: Path, step_name: str) -> Path:
        return base_dir / step_name

    def _step_output_path(self, base_dir: Path, step_name: str) -> Path:
        return base_dir / f"{step_name}.json"

    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _insert_step_result(
        self,
        *,
        step_name: str,
        step_type: str,
        status: str,
        resolved_input: Any,
        iteration_index: int | None,
        iteration_key: str | None,
        started_at: str,
    ) -> int:
        if self.run_id is None:
            raise PipelineError("pipeline run_id is required before execution")
        with self._state_lock:
            with self._db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO pipeline_step_results (
                        run_id, step_name, step_type, iteration_index, iteration_key,
                        status, input, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.run_id,
                        step_name,
                        step_type,
                        iteration_index,
                        iteration_key,
                        status,
                        json.dumps(resolved_input) if resolved_input is not None else None,
                        started_at,
                    ),
                )
                conn.commit()
                self._write_meta()
                return int(cursor.lastrowid)

    def _update_step_result_partial(
        self,
        row_id: int,
        *,
        output: Any | None = None,
        output_file: Path | None = None,
        error: str | None = None,
    ) -> None:
        with self._state_lock:
            with self._db() as conn:
                conn.execute(
                    """
                    UPDATE pipeline_step_results
                    SET output = ?, output_file = ?, error = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(output) if output is not None else None,
                        str(output_file) if output_file else None,
                        error,
                        row_id,
                    ),
                )
                conn.commit()
            self._write_meta()

    def _finish_step_result(self, row_id: int, *, status: str, output: Any, output_file: Path | None, finished_at: str, duration_ms: int, error: str | None) -> None:
        with self._state_lock:
            with self._db() as conn:
                conn.execute(
                    """
                    UPDATE pipeline_step_results
                    SET status = ?, output = ?, output_file = ?, finished_at = ?, duration_ms = ?, error = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        json.dumps(output) if output is not None else None,
                        str(output_file) if output_file else None,
                        finished_at,
                        duration_ms,
                        error,
                        row_id,
                    ),
                )
                conn.commit()
            self._write_meta()

    def _record_step_result(self, **kwargs: Any) -> None:
        row_id = self._insert_step_result(
            step_name=kwargs["step_name"],
            step_type=kwargs["step_type"],
            status=kwargs["status"],
            resolved_input=kwargs["resolved_input"],
            iteration_index=kwargs["iteration_index"],
            iteration_key=kwargs["iteration_key"],
            started_at=_utc_now(),
        )
        self._finish_step_result(
            row_id,
            status=kwargs["status"],
            output=kwargs["output"],
            output_file=kwargs["output_file"],
            finished_at=_utc_now(),
            duration_ms=kwargs["duration_ms"],
            error=kwargs["error"],
        )

    def _set_run_state(self, *, status: str | None = None, current_step: str | None = None, error: str | None = None, stats: dict[str, Any] | None = None) -> None:
        if self.run_id is None:
            return
        with self._state_lock:
            with self._db() as conn:
                run = conn.execute("SELECT status, current_step, error, stats FROM pipeline_runs WHERE id = ?", (self.run_id,)).fetchone()
                conn.execute(
                    """
                    UPDATE pipeline_runs
                    SET status = ?, current_step = ?, error = ?, stats = ?, finished_at = CASE WHEN ? IN ('completed', 'failed') THEN CURRENT_TIMESTAMP ELSE finished_at END
                    WHERE id = ?
                    """,
                    (
                        status or (run["status"] if run else "running"),
                        current_step,
                        error,
                        json.dumps(stats) if stats is not None else (run["stats"] if run else None),
                        status or (run["status"] if run else "running"),
                        self.run_id,
                    ),
                )
                conn.commit()
            self._write_meta(status=status, current_step=current_step, error=error, stats=stats)

    def _resolve_agent_tools(self, conn: sqlite3.Connection, agent: sqlite3.Row) -> list[dict[str, Any]]:
        tool_names = json.loads(agent["tools"]) if agent["tools"] else []
        if not tool_names:
            return []
        if not isinstance(tool_names, list):
            raise PipelineError(f"Agent '{agent['name']}' tools must be a JSON array")

        project_name = agent["project_name"]
        if not project_name:
            raise PipelineError(f"Agent '{agent['name']}' has tools configured but no bound project")

        placeholders = ", ".join("?" for _ in tool_names)
        rows = conn.execute(
            f"""
            SELECT tool_name, project, description, hook_point, return_type, known_side_effects, wrapper_path, input_schema, output_schema
            FROM tool_registry
            WHERE project = ? AND tool_name IN ({placeholders}) AND status = 'active'
            """,
            [project_name, *tool_names],
        ).fetchall()
        row_map = {row["tool_name"]: row for row in rows}
        missing = [name for name in tool_names if name not in row_map]
        if missing:
            raise PipelineError(
                f"Agent '{agent['name']}' references unregistered tools for project '{project_name}': {missing}"
            )

        tool_defs = []
        for name in tool_names:
            row = row_map[name]
            description = row["description"] or f"Wrapper {row['wrapper_path']} at {row['hook_point']}. Returns {row['return_type']}."
            if row["known_side_effects"]:
                description += f" Side effects: {row['known_side_effects']}."
            input_schema = {}
            output_schema = {}
            if row["input_schema"]:
                try:
                    input_schema = json.loads(row["input_schema"])
                except json.JSONDecodeError:
                    input_schema = {}
            if row["output_schema"]:
                try:
                    output_schema = json.loads(row["output_schema"])
                except json.JSONDecodeError:
                    output_schema = {}
            tool_defs.append(
                {
                    "name": row["tool_name"],
                    "project": row["project"],
                    "description": description,
                    "params": input_schema,
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                }
            )
        return tool_defs

    def _latest_step_row(self, step_name: str, iteration_index: int | None) -> sqlite3.Row | None:
        with self._db() as conn:
            if iteration_index is None:
                return conn.execute(
                    """
                    SELECT * FROM pipeline_step_results
                    WHERE run_id = ? AND step_name = ? AND iteration_index IS NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (self.run_id, step_name),
                ).fetchone()
            return conn.execute(
                """
                SELECT * FROM pipeline_step_results
                WHERE run_id = ? AND step_name = ? AND iteration_index = ?
                ORDER BY id DESC LIMIT 1
                """,
                (self.run_id, step_name, iteration_index),
            ).fetchone()

    def _load_payload(self, row: sqlite3.Row | None) -> Any:
        if row is None:
            return None
        if row["step_type"] == "human_gate" and row["output"]:
            return json.loads(row["output"])
        if row["output_file"] and os.path.exists(row["output_file"]):
            return json.loads(Path(row["output_file"]).read_text(encoding="utf-8"))
        if row["output"]:
            return json.loads(row["output"])
        return None

    def _load_iteration_step_state(self, step: StepSpec) -> dict[int, dict[str, dict[str, Any]]]:
        nested_names = [nested.name for nested in step.steps]
        if not nested_names:
            return {}
        placeholders = ", ".join("?" for _ in nested_names)
        with self._db() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM pipeline_step_results
                WHERE run_id = ? AND iteration_index IS NOT NULL AND step_name IN ({placeholders})
                ORDER BY id
                """,
                [self.run_id, *nested_names],
            ).fetchall()
        state: dict[int, dict[str, dict[str, Any]]] = {}
        for row in rows:
            index = int(row["iteration_index"])
            state.setdefault(index, {})[row["step_name"]] = {
                "status": row["status"],
                "payload": self._load_payload(row),
            }
        return state

    def _write_foreach_partial(
        self,
        row_id: int,
        foreach_dir: Path,
        results: list[dict[str, Any]],
        started: float,
        *,
        paused: bool,
    ) -> None:
        summary = {
            "total": len(results),
            "completed": len([entry for entry in results if entry.get("status") == "completed"]),
            "failed": len([entry for entry in results if entry.get("status") == "failed"]),
            "paused": len([entry for entry in results if entry.get("status") == "paused"]),
            "results": results,
        }
        output_path = foreach_dir / "_results.json"
        output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        self._finish_step_result(
            row_id,
            status="paused" if paused else "completed",
            output=summary,
            output_file=output_path,
            finished_at=_utc_now(),
            duration_ms=int((time.time() - started) * 1000),
            error=None,
        )

    def _final_stats(self, *, status: str) -> dict[str, Any]:
        duration = 0.0
        if self._started_monotonic is not None:
            duration = time.monotonic() - self._started_monotonic
        return {
            "steps_completed": self._steps_completed,
            "steps_failed": self._steps_failed,
            "steps_total": self._steps_total,
            "duration_seconds": round(duration, 3),
            "status": status,
        }

    def _write_meta(self, *, status: str | None = None, current_step: str | None = None, error: str | None = None, stats: dict[str, Any] | None = None) -> None:
        with self._state_lock:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "pipeline": self.spec.name,
                "run_id": self.run_id,
                "run_name": self.run_name,
                "input": self.input_data,
                "status": status,
                "current_step": current_step,
                "error": error,
                "stats": stats,
                "updated_at": _utc_now(),
            }
            (self.output_dir / "_meta.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _normalize_watcher_items(self, step: StepSpec, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
        raise PipelineError(
            f"watcher step '{step.name}' poll tool '{step.poll_tool}' must return a list or an object with an 'items' list"
        )

    def _watcher_item_key(self, step: StepSpec, item: Any, fallback_index: int) -> str:
        if isinstance(item, dict):
            value = item.get(step.item_key or "id")
            if value is not None:
                return str(value)
        return _iteration_key(item, fallback_index)

    def _agent_workstation(self, agent_name: str | None) -> str | None:
        if not agent_name:
            return None
        try:
            from custodian.services.workstations import get_agent_workstation

            return get_agent_workstation(agent_name)
        except Exception:
            return None

    def _agent_has_workstation(self, agent_name: str | None) -> bool:
        return bool(self._agent_workstation(agent_name))

    def _first_workstation_agent(self, steps: list[StepSpec]) -> str | None:
        for nested_step in steps:
            if nested_step.agent and self._agent_has_workstation(nested_step.agent):
                return nested_step.agent
            nested = self._first_workstation_agent(nested_step.steps)
            if nested:
                return nested
        return None

    def _render_task_template(self, template: str, item: Any, index: int) -> str:
        values: dict[str, Any] = {"item": item, "index": index}
        if isinstance(item, dict):
            values.update(item)
        return template.format(**values)


def _ensure_unique_step_names(steps: list[StepSpec], seen: set[str] | None = None) -> None:
    seen = seen or set()
    for step in steps:
        if step.name in seen:
            raise PipelineError(f"duplicate step name '{step.name}'")
        seen.add(step.name)
        _ensure_unique_step_names(step.steps, seen)


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    schema_type = schema.get("type")
    required = bool(schema.get("required"))
    if value is None:
        if required:
            errors.append(f"missing required field: {path}")
        return errors
    if schema_type == "string":
        if not isinstance(value, str):
            errors.append(f"field '{path}' expected string, got {type(value).__name__}")
    elif schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"field '{path}' expected integer, got {type(value).__name__}")
    elif schema_type == "boolean":
        if not isinstance(value, bool):
            errors.append(f"field '{path}' expected boolean, got {type(value).__name__}")
    elif schema_type == "list":
        if not isinstance(value, list):
            errors.append(f"field '{path}' expected list, got {type(value).__name__}")
        else:
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    if _looks_like_field_map(item_schema):
                        if not isinstance(item, dict):
                            errors.append(f"field '{path}[{index}]' expected object, got {type(item).__name__}")
                        else:
                            for nested_name, nested_schema in item_schema.items():
                                if isinstance(nested_schema, dict):
                                    errors.extend(_validate_schema_value(item.get(nested_name), nested_schema, f"{path}[{index}].{nested_name}"))
                    else:
                        errors.extend(_validate_schema_value(item, item_schema, f"{path}[{index}]"))
    elif schema_type == "object":
        if not isinstance(value, dict):
            errors.append(f"field '{path}' expected object, got {type(value).__name__}")
    return errors


def _looks_like_field_map(schema: dict[str, Any]) -> bool:
    if not schema:
        return False
    return all(isinstance(value, dict) and "type" in value for value in schema.values())


def _traverse_path(current: Any, parts: list[str], full_ref: str, root_name: str) -> Any:
    value = current
    traversed = root_name
    for part in parts:
        if isinstance(value, dict):
            if part not in value:
                raise PipelineError(f"$ref resolution failed: '{full_ref}' — key '{part}' not found in '{traversed}'")
            value = value[part]
        elif isinstance(value, list):
            if not part.isdigit():
                raise PipelineError(f"$ref resolution failed: '{full_ref}' — expected numeric list index at '{traversed}', got '{part}'")
            index = int(part)
            if index >= len(value):
                raise PipelineError(f"$ref resolution failed: '{full_ref}' — index {index} out of range in '{traversed}'")
            value = value[index]
        else:
            raise PipelineError(
                f"$ref resolution failed: '{full_ref}' — cannot traverse '{part}' into {type(value).__name__} at '{traversed}'"
            )
        traversed = f"{traversed}.{part}"
    return value


def _call_bridge(bridge_url: str, project: str, tool_name: str, params: dict[str, Any]) -> Any:
    payload = {
        "project": project,
        "tool_name": tool_name,
        "params": params,
    }
    request = Request(
        bridge_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PipelineError(f"tool step bridge call failed for '{tool_name}' on '{project}': HTTP {exc.code} {body}") from exc
    except URLError as exc:
        raise PipelineError(f"tool step bridge call failed for '{tool_name}' on '{project}': {exc.reason}") from exc
    if isinstance(data, dict) and "result" in data and len(data) == 1:
        return data["result"]
    return data


def _iteration_key(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("brand", "name", "id"):
            value = item.get(key)
            if value:
                return str(value)
    return str(index)


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return slug or "item"


def _default_run_name() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _count_steps(steps: list[StepSpec]) -> int:
    count = 0
    for step in steps:
        count += 1
        if step.type in {"foreach", "watcher"}:
            count += _count_steps(step.steps)
    return count
