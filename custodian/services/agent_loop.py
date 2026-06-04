from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any

import httpx


def _chat_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1]
    if ":" in model:
        return model.split(":", 1)[1]
    return model


def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("input_schema") or tool.get("params") or {"type": "object", "properties": {}},
        },
    }


def _tool_protocol(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return ""
    manifest = [
        {
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "input_schema": tool.get("input_schema") or tool.get("params") or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]
    return (
        "\n\nAvailable local tools are exposed as function tools. "
        "If native tool calling is unavailable, request a tool by responding with ONLY JSON like "
        '{"tool_call":{"name":"tool_name","params":{...}}}. '
        "After receiving a tool result, produce the final answer as normal text.\n"
        f"Tool manifest:\n{json.dumps(manifest, indent=2)}"
    )


async def _call_proxy(
    *,
    proxy_url: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": _chat_model_name(model),
        "messages": messages,
    }
    if tools:
        payload["tools"] = [_tool_schema(tool) for tool in tools]
        payload["tool_choice"] = "auto"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(proxy_url, json=payload)
            if response.status_code in {429, 500, 502, 503, 504}:
                response.raise_for_status()
            response.raise_for_status()
            decoded = response.json()
            return decoded["choices"][0]["message"]
        except Exception as exc:  # noqa: BLE001 - retryable proxy boundary
            last_error = exc
            if attempt == 2:
                break
            await asyncio.sleep(2**attempt)
    raise RuntimeError(f"LLM proxy request failed after 3 attempts: {last_error}")


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return str(content)


def _content_tool_uses(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in {"tool_use", "function_call"}:
            continue
        name = item.get("name") or item.get("function", {}).get("name")
        arguments = item.get("input") or item.get("arguments") or item.get("function", {}).get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        calls.append({"id": str(item.get("id") or name), "name": str(name or ""), "arguments": arguments, "native": True})
    return calls


def _message_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        calls.append({"id": str(call.get("id") or function.get("name") or "tool_call"), "name": str(function.get("name") or ""), "arguments": arguments, "native": True})
    calls.extend(_content_tool_uses(message.get("content")))
    if not calls:
        text_call = _text_tool_call(_content_text(message.get("content")))
        if text_call:
            calls.append(text_call)
    return calls


def _text_tool_call(text: str) -> dict[str, Any] | None:
    try:
        decoded = _extract_json_object(text)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    tool_call = decoded.get("tool_call")
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        params = tool_call.get("params") or tool_call.get("arguments") or {}
        if isinstance(name, str) and isinstance(params, dict):
            return {"id": name, "name": name, "arguments": params, "native": False}
    if isinstance(decoded.get("name"), str) and isinstance(decoded.get("params"), dict):
        return {"id": decoded["name"], "name": decoded["name"], "arguments": decoded["params"], "native": False}
    return None


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


def _final_response_text(text: str) -> str:
    try:
        decoded = _extract_json_object(text)
    except ValueError:
        return text
    if not isinstance(decoded, dict):
        return text
    final_answer = decoded.get("final_answer")
    if isinstance(final_answer, str):
        return final_answer
    if isinstance(final_answer, dict):
        return json.dumps(final_answer, sort_keys=True)
    response = decoded.get("response") or decoded.get("answer")
    return str(response) if response is not None else text


def _format_command(template: str, params: dict[str, Any]) -> str:
    values = {key: shlex.quote(str(value)) for key, value in params.items()}
    values["input"] = shlex.quote(json.dumps(params, sort_keys=True))
    field_names = [name for _, name, _, _ in Formatter().parse(template) if name]
    missing = [name for name in field_names if name not in values]
    if missing:
        raise ValueError(f"missing tool argument(s): {', '.join(missing)}")
    return template.format(**values)


async def _execute_tool(tool: dict[str, Any], params: dict[str, Any], working_dir: str) -> dict[str, Any]:
    template = tool.get("command_template") or tool.get("handler")
    if not template:
        return {"ok": False, "error": "tool has no command_template or handler"}
    try:
        command = _format_command(str(template), params)
    except Exception as exc:  # noqa: BLE001 - returned to model as tool result
        return {"ok": False, "error": str(exc)}
    timeout = int(tool.get("timeout") or 60)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-lc", command],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"tool timed out after {timeout}s", "stdout": exc.stdout or "", "stderr": exc.stderr or ""}
    except Exception as exc:  # noqa: BLE001 - returned to model as tool result
        return {"ok": False, "error": str(exc)}

    payload = {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-4000:],
    }
    if result.returncode != 0 and not payload["stderr"]:
        payload["stderr"] = payload["stdout"]
    return payload


async def run_agent_loop(
    task: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    model: str = "gpt-5.4",
    proxy_url: str = "http://127.0.0.1:4096/v1/chat/completions",
    working_dir: str = ".",
    output_dir: str = "./output",
    max_turns: int = 30,
) -> dict[str, Any]:
    """Run a workstation-local tool-use loop until the model returns final text."""

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    tool_map = {str(tool.get("name")): tool for tool in tools if tool.get("name")}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt + _tool_protocol(tools)},
        {"role": "user", "content": task},
    ]
    tool_calls_made: list[dict[str, Any]] = []

    for _turn in range(max(1, int(max_turns))):
        message = await _call_proxy(proxy_url=proxy_url, model=model, messages=messages, tools=tools)
        calls = _message_tool_calls(message)
        if calls:
            messages.append(message)
            for call in calls:
                tool = tool_map.get(call["name"])
                if tool is None:
                    result = {"ok": False, "error": f"tool '{call['name']}' is not available"}
                else:
                    result = await _execute_tool(tool, call.get("arguments") or {}, working_dir)
                tool_calls_made.append({"name": call["name"], "arguments": call.get("arguments") or {}, "result": result})
                if call.get("native"):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": json.dumps(result, sort_keys=True),
                        }
                    )
                else:
                    messages.append({"role": "user", "content": f"[Tool Result: {call['name']}]\n{json.dumps(result, sort_keys=True)}"})
            continue

        result = {
            "task": task,
            "response": _final_response_text(_content_text(message.get("content"))),
            "tool_calls_made": tool_calls_made,
            "model": model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        Path(output_dir, "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    result = {
        "task": task,
        "response": "",
        "tool_calls_made": tool_calls_made,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": f"max_turns exceeded ({max_turns})",
    }
    Path(output_dir, "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    raise RuntimeError(result["error"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    args = parser.parse_args()
    task_data = json.loads(Path(args.task_file).read_text(encoding="utf-8"))
    result = asyncio.run(run_agent_loop(**task_data))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
