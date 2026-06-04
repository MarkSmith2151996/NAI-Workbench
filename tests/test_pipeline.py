from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custodian.pipeline import PipelineRun, PipelineSpec, PipelineError, RefResolver


FIXTURE = Path(__file__).resolve().parents[1] / "custodian" / "pipelines" / "fba-brand-analysis.yaml"


def create_test_db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pipeline_runs (
            id INTEGER PRIMARY KEY,
            pipeline_id INTEGER,
            run_name TEXT NOT NULL,
            input TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            current_step TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            error TEXT,
            stats TEXT
        );
        CREATE TABLE pipeline_step_results (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            step_type TEXT NOT NULL,
            iteration_index INTEGER,
            iteration_key TEXT,
            status TEXT DEFAULT 'pending',
            input TEXT,
            output TEXT,
            output_file TEXT,
            started_at TEXT,
            finished_at TEXT,
            duration_ms INTEGER,
            error TEXT
        );
        CREATE TABLE agents (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            system_prompt TEXT NOT NULL,
            model TEXT DEFAULT 'openai/gpt-5.4',
            project_id INTEGER,
            max_turns INTEGER DEFAULT 20,
            tools TEXT,
            mcp_servers TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
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
        CREATE TABLE tool_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            project TEXT NOT NULL,
            source_module TEXT NOT NULL,
            source_class TEXT,
            source_method TEXT,
            hook_point TEXT NOT NULL,
            return_type TEXT NOT NULL,
            known_side_effects TEXT,
            wrapper_path TEXT NOT NULL,
            created_by TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tool_name, project)
        );
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY,
            agent_id INTEGER,
            pipeline_id INTEGER,
            pipeline_step TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            input TEXT,
            output TEXT,
            tokens_used INTEGER,
            error TEXT,
            triggered_by TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO pipeline_runs (id, pipeline_id, run_name, input, output_dir, status) VALUES (1, 1, 'run_test', '{}', ?, 'running')",
        (str(path.parent / "outputs"),),
    )
    conn.commit()
    conn.close()
    return str(path)


def test_spec_parse() -> None:
    spec = PipelineSpec.from_yaml(FIXTURE.read_text(encoding="utf-8"))
    assert spec.name == "fba-brand-analysis"
    assert len(spec.steps) == 1
    step = spec.steps[0]
    assert step.name == "process_brands"
    assert step.type == "foreach"
    assert [nested.name for nested in step.steps] == ["screen", "download", "analyze", "score", "write"]
    assert [nested.type for nested in step.steps] == ["agent", "human_gate", "tool", "tool", "tool"]


def test_spec_parse_parallel_and_watcher() -> None:
    spec = PipelineSpec.from_yaml(
        """
name: watcher-pipeline
version: 1
description: test
trigger: manual
input_schema: {}
steps:
  - name: research
    type: foreach
    over: "[1, 2, 3]"
    as: item
    parallel: 3
    steps:
      - name: run_tool
        type: tool
        project: demo
        tool: worker
        input: {}
        output: result
  - name: escalate
    type: watcher
    watch: research
    poll_tool: list_items
    poll_project: demo
    poll_input: {}
    poll_interval: 0
    item_key: brand
    parallel: 2
    steps:
      - name: handle_item
        type: tool
        project: demo
        tool: handle
        input: {}
        output: result
"""
    )
    assert spec.steps[0].parallel == 3
    assert spec.steps[1].type == "watcher"
    assert spec.steps[1].watch == "research"
    assert spec.steps[1].poll_tool == "list_items"
    assert spec.steps[1].parallel == 2


def test_ref_resolver() -> None:
    resolver = RefResolver([{"input": {"brands": [{"brand": "Corelle", "keepa_url": "u"}]} }])
    resolver.set("download", {"csv_result": {"csv_path": "/tmp/x.csv"}})
    resolver.set("analyze", {"analysis": {"products": [1, 2, 3]}})
    child = resolver.child()
    child.set("brand", {"keepa_url": "https://keepa"})

    assert resolver.resolve("$input.brands")[0]["brand"] == "Corelle"
    assert resolver.resolve("$download.csv_result.csv_path") == "/tmp/x.csv"
    assert resolver.resolve({"products": "$analyze.analysis.products"}) == {"products": [1, 2, 3]}
    assert child.resolve("$brand.keepa_url") == "https://keepa"
    with pytest.raises(PipelineError):
        resolver.resolve("$analyze.analysis.missing")


def test_input_validation() -> None:
    spec = PipelineSpec.from_yaml(FIXTURE.read_text(encoding="utf-8"))
    assert spec.validate_input({"brands": [{"brand": "Corelle", "keepa_url": "https://keepa"}]}) == []
    errors = spec.validate_input({"brands": [{"brand": "Corelle"}]})
    assert any("keepa_url" in error for error in errors)


def test_foreach_collects_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(FIXTURE.read_text(encoding="utf-8"))
    db_path = create_test_db(tmp_path / "test.db")
    output_base = tmp_path / "outputs"

    async def fake_execute_tool_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        resolved_input = context.resolve(step.input)
        if step.name == "analyze":
            result = {"products": [{"sku": iteration_key}]}
        elif step.name == "score":
            result = {"scored_products": [{"sku": iteration_key, "score": 1}]}
        else:
            result = {"written": True, "dry_run": resolved_input["dry_run"]}
        context.set(step.name, {step.output: result})
        return result

    async def fake_execute_agent_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        result = {"verdict": "GO"}
        context.set(step.name, {step.output: result})
        return result

    async def fake_execute_human_gate_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        result = {"csv_path": f"/tmp/{iteration_key}.csv"}
        context.set(step.name, result)
        return result

    monkeypatch.setattr(PipelineRun, "execute_tool_step", fake_execute_tool_step)
    monkeypatch.setattr(PipelineRun, "execute_agent_step", fake_execute_agent_step)
    monkeypatch.setattr(PipelineRun, "execute_human_gate_step", fake_execute_human_gate_step)
    run = PipelineRun(
        spec,
        {"brands": [{"brand": "corelle", "keepa_url": "a"}, {"brand": "cambro", "keepa_url": "b"}]},
        db_path,
        str(output_base),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(output_base / spec.name / "run_test"),
    )
    result = asyncio.run(run.execute())
    assert result["status"] == "completed"
    payload = json.loads((output_base / spec.name / "run_test" / "process_brands" / "_results.json").read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert payload[0]["status"] == "completed"
    assert payload[1]["write"]["written"] is True


def test_foreach_parallel_tool_steps_use_real_concurrency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: parallel-tools
version: 1
description: test
trigger: manual
input_schema:
  items:
    type: list
steps:
  - name: process
    type: foreach
    over: "$input.items"
    as: item
    parallel: 3
    steps:
      - name: sleep_tool
        type: tool
        project: demo
        tool: sleeper
        input:
          value: "$item"
        output: result
"""
    )
    db_path = create_test_db(tmp_path / "parallel.db")
    calls: list[int] = []

    def fake_bridge(bridge_url: str, project: str, tool_name: str, params: dict[str, object]) -> dict[str, object]:
        time.sleep(0.2)
        calls.append(int(params["value"]))
        return {"value": params["value"]}

    monkeypatch.setattr("custodian.pipeline._call_bridge", fake_bridge)
    run = PipelineRun(
        spec,
        {"items": [1, 2, 3, 4, 5, 6]},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    started = time.perf_counter()
    result = asyncio.run(run.execute())
    elapsed = time.perf_counter() - started

    assert result["status"] == "completed"
    assert sorted(calls) == [1, 2, 3, 4, 5, 6]
    assert elapsed < 0.9


def test_watcher_dispatches_before_foreach_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: watcher-live
version: 1
description: test
trigger: manual
input_schema:
  brands:
    type: list
steps:
  - name: research
    type: foreach
    over: "$input.brands"
    as: brand
    steps:
      - name: research_brand
        type: tool
        project: demo
        tool: research_brand
        input:
          brand: "$brand.brand"
        output: result
  - name: escalation_watcher
    type: watcher
    watch: research
    poll_tool: list_escalations
    poll_project: demo
    poll_input: {}
    poll_interval: 0
    item_key: brand
    parallel: 2
    steps:
      - name: escalate_brand
        type: tool
        project: demo
        tool: escalate_brand
        input:
          brand: "$item.brand"
        output: result
"""
    )
    db_path = create_test_db(tmp_path / "watcher.db")
    escalations: list[dict[str, str]] = []
    call_times: dict[str, float] = {}
    research_completed = 0

    def fake_bridge(bridge_url: str, project: str, tool_name: str, params: dict[str, object]) -> object:
        nonlocal research_completed
        if tool_name == "research_brand":
            brand = str(params["brand"])
            time.sleep(0.05)
            escalations.append({"brand": brand})
            research_completed += 1
            if research_completed == 3:
                call_times["research_done"] = time.perf_counter()
            return {"brand": brand, "status": "researched"}
        if tool_name == "list_escalations":
            return list(escalations)
        if tool_name == "escalate_brand":
            brand = str(params["brand"])
            call_times.setdefault(f"escalate_{brand}", time.perf_counter())
            return {"brand": brand, "status": "escalated"}
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr("custodian.pipeline._call_bridge", fake_bridge)
    run = PipelineRun(
        spec,
        {"brands": [{"brand": "alpha"}, {"brand": "beta"}, {"brand": "gamma"}]},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    result = asyncio.run(run.execute())

    assert result["status"] == "completed"
    assert "research_done" in call_times
    assert any(key.startswith("escalate_") for key in call_times)
    assert min(value for key, value in call_times.items() if key.startswith("escalate_")) < call_times["research_done"]

    watcher_results = json.loads(
        (tmp_path / "outputs" / spec.name / "run_test" / "escalation_watcher" / "_results.json").read_text(encoding="utf-8")
    )
    assert [entry["key"] for entry in watcher_results] == ["alpha", "beta", "gamma"]


def test_resume_skips_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(FIXTURE.read_text(encoding="utf-8"))
    db_path = create_test_db(tmp_path / "resume.db")
    output_root = tmp_path / "outputs" / spec.name / "run_test"
    foreach_dir = output_root / "process_brands"
    foreach_dir.mkdir(parents=True, exist_ok=True)
    existing_results = [
        {"index": 0, "key": "corelle", "status": "completed", "download": {"csv_path": "/tmp/corelle.csv"}},
        {"index": 1, "key": "cambro", "status": "failed", "error": "boom"},
    ]
    (foreach_dir / "_results.json").write_text(json.dumps(existing_results, indent=2), encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE pipeline_runs SET input = ?, output_dir = ?, status = 'failed', current_step = 'process_brands' WHERE id = 1",
        (json.dumps({"brands": [{"brand": "corelle", "keepa_url": "a"}, {"brand": "cambro", "keepa_url": "b"}]}), str(output_root)),
    )
    conn.execute(
        "INSERT INTO pipeline_step_results (run_id, step_name, step_type, status, output, output_file) VALUES (1, 'process_brands', 'foreach', 'failed', ?, ?)",
        (json.dumps({"results": existing_results}), str(foreach_dir / "_results.json")),
    )
    conn.commit()
    conn.close()

    calls: list[str] = []

    async def fake_execute_tool_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        calls.append(f"{step.name}:{iteration_index}:{iteration_key}")
        result = {step.output: {"ok": True}}
        context.set(step.name, result)
        return result[step.output]

    async def fake_execute_agent_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        result = {"verdict": "GO"}
        context.set(step.name, {step.output: result})
        return result

    async def fake_execute_human_gate_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        result = {"csv_path": f"/tmp/{iteration_key}.csv"}
        context.set(step.name, result)
        return result

    monkeypatch.setattr(PipelineRun, "execute_tool_step", fake_execute_tool_step)
    monkeypatch.setattr(PipelineRun, "execute_agent_step", fake_execute_agent_step)
    monkeypatch.setattr(PipelineRun, "execute_human_gate_step", fake_execute_human_gate_step)
    run = PipelineRun(
        spec,
        {"brands": [{"brand": "corelle", "keepa_url": "a"}, {"brand": "cambro", "keepa_url": "b"}]},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(output_root),
    )
    result = asyncio.run(run.resume())
    assert result["status"] == "completed"
    assert all(":0:" not in call for call in calls)
    assert any(":1:" in call for call in calls)


def test_agent_step_calls_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: agent-pipeline
version: 1
description: test
trigger: manual
input_schema:
  brand:
    type: string
    required: true
steps:
  - name: screen
    type: agent
    agent: test-echo
    input:
      brand: "$input.brand"
    output: verdict
"""
    )
    db_path = create_test_db(tmp_path / "agent.db")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO projects (id, name, path, status) VALUES (1, 'fba-command-center', '/tmp/fba', 'active')")
    conn.execute(
        "INSERT INTO agents (id, name, system_prompt, model, project_id, max_turns, tools, status) VALUES (1, 'test-echo', 'Echo input.', 'openai/gpt-5.4', 1, 5, '[]', 'active')"
    )
    conn.commit()
    conn.close()

    captured: dict[str, object] = {}

    def fake_compile_prompt(*, system_prompt, tools, input_data, db=None):
        captured["system_prompt"] = system_prompt
        captured["tools"] = tools
        captured["input_data"] = input_data
        captured["db"] = db
        return {"system": system_prompt, "user": json.dumps(input_data)}

    async def fake_run_agent_loop(*, model, compiled_prompt, max_turns, tools, bridge_url):
        from custodian.executor import AgentLoopResult

        captured["model"] = model
        captured["compiled_prompt"] = compiled_prompt
        captured["max_turns"] = max_turns
        captured["bridge_url"] = bridge_url
        return AgentLoopResult(output={"verdict": "GO", "brand": "Corelle"}, tokens_used=42)

    monkeypatch.setattr("custodian.compiler.compile_prompt", fake_compile_prompt)
    monkeypatch.setattr("custodian.executor.run_agent_loop", fake_run_agent_loop)

    run = PipelineRun(
        spec,
        {"brand": "Corelle"},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    result = asyncio.run(run.execute())
    assert result["status"] == "completed"
    payload = json.loads((tmp_path / "outputs" / spec.name / "run_test" / "screen.json").read_text(encoding="utf-8"))
    assert payload == {"verdict": "GO", "brand": "Corelle"}
    assert captured["input_data"] == {"brand": "Corelle"}
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT output, tokens_used, status FROM agent_runs WHERE agent_id = 1").fetchone()
    conn.close()
    assert json.loads(row[0]) == {"verdict": "GO", "brand": "Corelle"}
    assert row[1] == 42
    assert row[2] == "completed"


def test_human_gate_pauses_run(tmp_path: Path) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: gate-pipeline
version: 1
description: test
trigger: manual
input_schema: {}
steps:
  - name: wait_for_csv
    type: human_gate
    description: "Provide CSV path"
    awaiting:
      csv_path:
        type: string
        required: true
"""
    )
    db_path = create_test_db(tmp_path / "gate.db")
    run = PipelineRun(
        spec,
        {},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    result = asyncio.run(run.execute())
    assert result["status"] == "paused"
    assert result["waiting_for"] == "wait_for_csv"
    conn = sqlite3.connect(db_path)
    run_row = conn.execute("SELECT status, current_step FROM pipeline_runs WHERE id = 1").fetchone()
    gate_row = conn.execute("SELECT status FROM pipeline_step_results WHERE step_name = 'wait_for_csv'").fetchone()
    conn.close()
    assert run_row == ("paused", "wait_for_csv")
    assert gate_row == ("waiting",)


def test_resume_from_gate(tmp_path: Path) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: resume-gate
version: 1
description: test
trigger: manual
input_schema: {}
steps:
  - name: wait_for_csv
    type: human_gate
    description: "Provide CSV path"
    awaiting:
      csv_path:
        type: string
        required: true
  - name: analyze
    type: tool
    project: fba-command-center
    tool: keepa_analyze
    input:
      csv_path: "$wait_for_csv.csv_path"
    output: analysis
"""
    )
    db_path = create_test_db(tmp_path / "resume-gate.db")
    run = PipelineRun(
        spec,
        {},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    paused = asyncio.run(run.execute())
    assert paused["status"] == "paused"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE pipeline_step_results SET status = 'completed', output = ?, finished_at = CURRENT_TIMESTAMP WHERE step_name = 'wait_for_csv'",
        (json.dumps({"csv_path": "/tmp/manual.csv"}),),
    )
    conn.execute("UPDATE pipeline_runs SET status = 'running' WHERE id = 1")
    conn.commit()
    conn.close()

    async def fake_execute_tool_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        resolved = context.resolve(step.input)
        assert resolved["csv_path"] == "/tmp/manual.csv"
        result = {"products": [{"sku": "abc"}]}
        context.set(step.name, {step.output: result})
        return result

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(PipelineRun, "execute_tool_step", fake_execute_tool_step)
    resumed = PipelineRun(
        spec,
        {},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    result = asyncio.run(resumed.resume())
    monkeypatch.undo()
    assert result["status"] == "completed"


def test_gate_in_foreach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: foreach-gate
version: 1
description: test
trigger: manual
input_schema:
  brands:
    type: list
    items:
      brand:
        type: string
        required: true
steps:
  - name: process
    type: foreach
    over: "$input.brands"
    as: brand
    steps:
      - name: screen
        type: agent
        agent: test-echo
        input:
          brand: "$brand.brand"
        output: verdict
      - name: manual_download
        type: human_gate
        description: "Provide CSV path"
        awaiting:
          csv_path:
            type: string
            required: true
      - name: analyze
        type: tool
        project: fba-command-center
        tool: keepa_analyze
        input:
          csv_path: "$manual_download.csv_path"
        output: analysis
"""
    )
    db_path = create_test_db(tmp_path / "foreach-gate.db")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO projects (id, name, path, status) VALUES (1, 'fba-command-center', '/tmp/fba', 'active')")
    conn.execute(
        "INSERT INTO agents (id, name, system_prompt, model, project_id, max_turns, tools, status) VALUES (1, 'test-echo', 'Echo input.', 'openai/gpt-5.4', 1, 5, '[]', 'active')"
    )
    conn.commit()
    conn.close()

    async def fake_run_agent_loop(*, model, compiled_prompt, max_turns, tools, bridge_url):
        from custodian.executor import AgentLoopResult

        brand = json.loads(compiled_prompt["user"])["brand"]
        return AgentLoopResult(output={"verdict": "GO", "brand": brand})

    monkeypatch.setattr("custodian.compiler.compile_prompt", lambda **kwargs: {"system": kwargs["system_prompt"], "user": json.dumps(kwargs["input_data"])})
    monkeypatch.setattr("custodian.executor.run_agent_loop", fake_run_agent_loop)

    calls: list[str] = []

    async def fake_execute_tool_step(self, step, context, base_dir, iteration_index=None, iteration_key=None):
        calls.append(f"{iteration_index}:{context.resolve(step.input)['csv_path']}")
        result = {"products": [{"sku": iteration_key}]}
        context.set(step.name, {step.output: result})
        return result

    monkeypatch.setattr(PipelineRun, "execute_tool_step", fake_execute_tool_step)

    run = PipelineRun(
        spec,
        {"brands": [{"brand": "corelle"}, {"brand": "cambro"}]},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    paused = asyncio.run(run.execute())
    assert paused["status"] == "paused"
    assert paused["iteration_index"] == 0

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE pipeline_step_results SET status = 'completed', output = ?, finished_at = CURRENT_TIMESTAMP WHERE step_name = 'manual_download' AND iteration_index = 0",
        (json.dumps({"csv_path": "/tmp/corelle.csv"}),),
    )
    conn.execute("UPDATE pipeline_runs SET status = 'running' WHERE id = 1")
    conn.commit()
    conn.close()

    resumed = PipelineRun(
        spec,
        {"brands": [{"brand": "corelle"}, {"brand": "cambro"}]},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    paused_again = asyncio.run(resumed.resume())
    assert paused_again["status"] == "paused"
    assert paused_again["iteration_index"] == 1
    assert calls == ["0:/tmp/corelle.csv"]


def _proxy_reachable() -> bool:
    try:
        request = urlopen
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            "http://127.0.0.1:4096/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "gpt-5.4",
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request(req, timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _proxy_reachable(), reason="OpenCode proxy is not reachable")
def test_live_agent_then_gate_flow(tmp_path: Path) -> None:
    spec = PipelineSpec.from_yaml(
        """
name: live-agent-gate
version: 1
description: test
trigger: manual
input_schema:
  brand:
    type: string
    required: true
steps:
  - name: screen
    type: agent
    agent: test-echo
    input:
      brand: "$input.brand"
    output: verdict
  - name: gate
    type: human_gate
    description: "Provide notes"
    awaiting:
      notes:
        type: string
        required: true
"""
    )
    db_path = create_test_db(tmp_path / "live.db")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO projects (id, name, path, status) VALUES (1, 'fba-command-center', '/tmp/fba', 'active')")
    conn.execute(
        "INSERT INTO agents (id, name, system_prompt, model, project_id, max_turns, tools, status) VALUES (1, 'test-echo', 'You are a test agent. Return only JSON in the form {\"final_answer\": {\"brand\": <brand>, \"verdict\": \"GO\"}} based on the input payload.', 'openai/gpt-5.4', 1, 5, '[]', 'active')"
    )
    conn.commit()
    conn.close()

    run = PipelineRun(
        spec,
        {"brand": "Corelle"},
        db_path,
        str(tmp_path / "outputs"),
        pipeline_id=1,
        run_id=1,
        run_name="run_test",
        output_dir=str(tmp_path / "outputs" / spec.name / "run_test"),
    )
    result = asyncio.run(run.execute())
    assert result["status"] == "paused"
    assert result["waiting_for"] == "gate"
