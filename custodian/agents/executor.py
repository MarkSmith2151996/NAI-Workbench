from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
import subprocess
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from custodian.agents.prompt_compiler import compile_prompt
from custodian.agents.schema import AgentSpec, GenericStructuredResult, LlmAgentSpec, ServiceAgentSpec, get_schema
from custodian.services.tool_router import resolve_agent_tools, route_tool_call


MAX_TOOL_ITERATIONS = int(os.environ.get("CUSTODIAN_AGENT_MAX_TOOL_ITERATIONS", "10"))
DEFAULT_CHAT_COMPLETIONS_BASE_URL = os.environ.get(
    "CUSTODIAN_LLM_PROXY_BASE_URL", "http://127.0.0.1:4096/v1"
)
DEFAULT_TOOL_SERVER_PORT = 9100
PROJECTS_ROOT = Path(__file__).resolve().parents[2]
CUSTODIAN_DB_PATH = PROJECTS_ROOT / "custodian" / "custodian.db"


@dataclass(frozen=True)
class AgentInvocationResult:
    output: dict[str, Any]
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None


async def execute_agent(spec: AgentSpec, input_data: dict[str, Any], model_override: str | None = None) -> AgentInvocationResult:
    try:
        if isinstance(spec, ServiceAgentSpec):
            return await execute_service_agent(spec, input_data)
        return await execute_llm_agent(spec, input_data, model_override=model_override)
    except Exception:
        full_tb = traceback.format_exc()
        print(f"[agent_executor] FULL TRACEBACK:\n{full_tb}", file=sys.stderr)
        raise


async def execute_llm_agent(spec: LlmAgentSpec, input_payload: dict[str, Any], model_override: str | None = None) -> AgentInvocationResult:
    tool_resolution = resolve_agent_tools([*spec.tools, *spec.toolbox], spec.project)
    if not tool_resolution["all_found"]:
        raise RuntimeError(f"Agent requires tools that don't exist: {tool_resolution['missing']}")
    tool_defs = [
        {
            "name": item["name"],
            "description": _tool_description(item),
            "params": _tool_params(item),
            "source": item["source"],
        }
        for item in tool_resolution["resolved"]
    ]
    compiled = compile_prompt(spec, input_payload, tools=tool_defs)
    model = model_override or compiled["model"]
    messages = [
        {"role": "system", "content": compiled["system"]},
        {"role": "user", "content": compiled["user"]},
    ]
    base_url = _llm_base_url(spec)
    total_input_tokens = 0
    total_output_tokens = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        response_text, usage = await _call_llm(model, messages, base_url)
        total_input_tokens += int(usage.get("prompt_tokens") or 0)
        total_output_tokens += int(usage.get("completion_tokens") or 0)
        parsed = _parse_response(response_text)
        if parsed["type"] == "tool_call":
            tool_result = await _execute_tool(spec.project, parsed["name"], parsed["params"], tool_defs)
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": f"[Tool Result: {parsed['name']}]\n{json.dumps(tool_result, sort_keys=True)}"})
            continue
        if parsed["type"] == "final_answer":
            output_schema = compiled["output_schema"]
            validated = output_schema.model_validate(parsed["data"])
            total_tokens = total_input_tokens + total_output_tokens
            return AgentInvocationResult(
                output=validated.model_dump(),
                tokens_input=total_input_tokens,
                tokens_output=total_output_tokens,
                cost_usd=None if total_tokens == 0 else None,
            )
        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": f"Your response was not valid JSON in the required format. Parse error: {parsed['error']}"})

    raise RuntimeError(f"Agent {spec.name} hit max iterations ({MAX_TOOL_ITERATIONS})")


async def execute_service_agent(spec: ServiceAgentSpec, input_payload: dict[str, Any]) -> AgentInvocationResult:
    missing = [field for field in spec.input.required if field not in input_payload]
    if missing:
        raise ValueError(f"Missing required input for {spec.name}: {missing[0]}")
    response = await asyncio.to_thread(_post_service, spec, input_payload)
    schema = get_schema(spec.output)
    output = _coerce_service_output(schema, input_payload, response)
    return AgentInvocationResult(output=output.model_dump())


async def _fetch_all_tools(spec: LlmAgentSpec) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if spec.tools:
        project_defs = await _fetch_box_tools(spec.project, spec.tools)
        for tool in project_defs:
            tool["source"] = spec.project
        tools.extend(project_defs)
    if spec.toolbox:
        toolbox_defs = await _fetch_box_tools("toolbox", spec.toolbox)
        for tool in toolbox_defs:
            tool["source"] = "toolbox"
        tools.extend(toolbox_defs)
    return tools


async def _fetch_box_tools(project: str, allowlist: list[str]) -> list[dict[str, Any]]:
    response = await asyncio.to_thread(_request_box_tool_server, project, "GET", "/tools", None)
    tools = _extract_tool_list(response)
    allowlisted = [_normalize_tool_def(tool) for tool in tools if _tool_name(tool) in set(allowlist)]
    missing = [name for name in allowlist if name not in {tool["name"] for tool in allowlisted}]
    if missing:
        raise RuntimeError(f"Project {project} tool server did not expose allowlisted tools: {missing}")
    return allowlisted


def _llm_base_url(spec: LlmAgentSpec) -> str:
    runtime_base_url = spec.runtime.base_url if spec.runtime else None
    return (runtime_base_url or DEFAULT_CHAT_COMPLETIONS_BASE_URL).rstrip("/")


async def _call_llm(model: str, messages: list[dict[str, str]], base_url: str) -> tuple[str, dict[str, Any]]:
    body = json.dumps(
        {
            "model": _chat_model_name(model),
            "messages": messages,
            "max_tokens": 4096,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        response_body = await asyncio.to_thread(_read_http_response, request)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    try:
        decoded = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("LLM proxy returned invalid JSON") from exc

    try:
        content = decoded["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM proxy response did not include choices[0].message.content") from exc

    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "".join(parts)
    if not isinstance(content, str):
        raise RuntimeError("LLM proxy returned non-string message content")

    usage = decoded.get("usage") if isinstance(decoded.get("usage"), dict) else {}
    return content, usage


def _read_http_response(request: urllib.request.Request) -> str:
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def _chat_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1]
    if ":" in model:
        return model.split(":", 1)[1]
    return model


def _parse_response(text: str) -> dict[str, Any]:
    try:
        decoded = _extract_json_object(text)
    except ValueError as exc:
        return {"type": "invalid", "error": str(exc)}
    if not isinstance(decoded, dict):
        return {"type": "invalid", "error": "JSON response must be an object"}
    tool_call = decoded.get("tool_call")
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        params = tool_call.get("params", {})
        if isinstance(name, str) and isinstance(params, dict):
            return {"type": "tool_call", "name": name, "params": params}
    final_answer = decoded.get("final_answer")
    if isinstance(final_answer, dict):
        return {"type": "final_answer", "data": final_answer}
    return {"type": "final_answer", "data": decoded}


def _tool_description(item: dict[str, Any]) -> str:
    details = item.get("details")
    if not isinstance(details, dict):
        return ""
    metadata = details.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("description") or "")
    return str(details.get("description") or "")


def _tool_params(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details")
    if not isinstance(details, dict):
        return {}
    metadata = details.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("input_schema") or {}
    return details.get("input_schema") or {}


async def _execute_tool(project: str, tool_name: str, params: dict[str, Any], tool_defs: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        response = await route_tool_call(tool_name, params, project=project)
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(response, dict):
        return response
    if isinstance(response, list):
        text_items = [getattr(item, "text", None) for item in response if getattr(item, "text", None) is not None]
        return {"result": text_items[0] if len(text_items) == 1 else text_items}
    return {"result": response}


def _post_service(spec: ServiceAgentSpec, input_payload: dict[str, Any]) -> dict[str, Any]:
    base_url = os.environ.get(spec.service.url_env or "", spec.service.url_default).rstrip("/")
    endpoint = spec.service.endpoint if spec.service.endpoint.startswith("/") else f"/{spec.service.endpoint}"
    body = json.dumps(input_payload).encode("utf-8")
    request = urllib.request.Request(f"{base_url}{endpoint}", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=spec.service.timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{spec.name} service returned HTTP {exc.code}: {error_body}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError(f"{spec.name} service request failed: {exc}") from exc
    decoded = json.loads(response_body)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{spec.name} service returned a non-object JSON response")
    return decoded


def _coerce_service_output(schema: type[BaseModel], input_payload: dict[str, Any], response: dict[str, Any]) -> BaseModel:
    try:
        return schema.model_validate(response)
    except Exception:
        return GenericStructuredResult.model_validate(response)


def _extract_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no JSON object found")


def _request_box_tool_server(project: str, method: str, path: str, payload: dict[str, Any] | None) -> Any:
    port = _box_tool_port(project)
    url = f"http://127.0.0.1:{port}{path}"
    try:
        return _http_json_request(url, method, payload)
    except Exception as http_exc:
        try:
            return _docker_tool_request(project, port, method, path, payload)
        except Exception as docker_exc:
            raise RuntimeError(f"box tool server request failed via localhost ({http_exc}) and docker exec ({docker_exc})") from docker_exc


def _http_json_request(url: str, method: str, payload: dict[str, Any] | None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        response_body = response.read().decode("utf-8")
    return json.loads(response_body)


def _docker_tool_request(project: str, port: int, method: str, path: str, payload: dict[str, Any] | None) -> Any:
    command = ["docker", "exec", f"alpha-{project}", "curl", "-sS", "-X", method, "-H", "Content-Type: application/json"]
    if payload is not None:
        command.extend(["-d", json.dumps(payload)])
    command.append(f"http://127.0.0.1:{port}{path}")
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return json.loads(completed.stdout)


def _box_tool_port(project: str) -> int:
    env_name = f"BOX_TOOL_PORT_{project.upper().replace('-', '_')}"
    env_value = os.environ.get(env_name)
    if env_value:
        return int(env_value)
    db_port = _lookup_box_tool_port(project)
    if db_port is not None:
        return db_port
    return DEFAULT_TOOL_SERVER_PORT


def _lookup_box_tool_port(project: str) -> int | None:
    if not CUSTODIAN_DB_PATH.exists():
        return None
    conn = sqlite3.connect(CUSTODIAN_DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT pb.tool_server_port
            FROM project_boxes pb
            JOIN projects p ON p.id = pb.project_id
            WHERE p.name = ?
            """,
            (project,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _extract_tool_list(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        tools = response
    elif isinstance(response, dict) and isinstance(response.get("tools"), list):
        tools = response["tools"]
    else:
        raise RuntimeError("Tool server /tools response must be a list or object with tools list")
    return [tool for tool in tools if isinstance(tool, dict)]


def _normalize_tool_def(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _tool_name(tool),
        "description": str(tool.get("description") or tool.get("tool_description") or ""),
        "params": tool.get("params") or tool.get("input_schema") or {},
    }


def _tool_name(tool: dict[str, Any]) -> str | None:
    name = tool.get("name") or tool.get("tool_name")
    return name if isinstance(name, str) else None
