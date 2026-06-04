from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_CHAT_COMPLETIONS_URL = "http://127.0.0.1:4096/v1/chat/completions"
DEFAULT_BOX_BRIDGE_URL = "http://localhost:9099/call-tool"


@dataclass(frozen=True)
class AgentLoopResult:
    output: dict[str, Any]
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_used: int = 0


async def run_agent_loop(
    *,
    model: str,
    compiled_prompt: dict[str, str],
    max_turns: int,
    tools: list[dict[str, Any]],
    bridge_url: str = DEFAULT_BOX_BRIDGE_URL,
) -> AgentLoopResult:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": compiled_prompt["system"]},
        {"role": "user", "content": compiled_prompt["user"]},
    ]
    total_input = 0
    total_output = 0
    total_tokens = 0
    tool_map = {str(tool.get("name")): tool for tool in tools}

    for _ in range(max(1, int(max_turns))):
        response_text, usage = await _call_llm(model, messages)
        total_input += int(usage.get("prompt_tokens") or 0)
        total_output += int(usage.get("completion_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)
        parsed = _parse_response(response_text)

        if parsed["type"] == "tool_call":
            tool_result = await _execute_tool(tool_map, parsed["name"], parsed["params"], bridge_url)
            messages.append({"role": "assistant", "content": response_text})
            messages.append(
                {
                    "role": "user",
                    "content": f"[Tool Result: {parsed['name']}]\n{json.dumps(tool_result, sort_keys=True)}",
                }
            )
            continue

        if parsed["type"] == "final_answer":
            return AgentLoopResult(
                output=parsed["data"],
                tokens_input=total_input,
                tokens_output=total_output,
                tokens_used=total_tokens,
            )

        messages.append({"role": "assistant", "content": response_text})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your response was not valid JSON in the required format. "
                    "Respond with ONLY a tool_call JSON object or a final_answer JSON object. "
                    f"Parse error: {parsed['error']}"
                ),
            }
        )

    raise RuntimeError(f"Agent loop hit max iterations ({max_turns})")


async def _call_llm(model: str, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    body = json.dumps({"model": _chat_model_name(model), "messages": messages}).encode("utf-8")
    request = Request(
        DEFAULT_CHAT_COMPLETIONS_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        response_body = await asyncio.to_thread(_read_http_response, request)
    except (URLError, socket.timeout, TimeoutError) as exc:
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


def _read_http_response(request: Request) -> str:
    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def _chat_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1]
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
        return {"type": "invalid", "error": "tool_call must include string name and object params"}

    if isinstance(decoded.get("name"), str) and isinstance(decoded.get("params", {}), dict):
        return {"type": "tool_call", "name": decoded["name"], "params": decoded.get("params", {})}

    final_answer = decoded.get("final_answer")
    if isinstance(final_answer, dict):
        return {"type": "final_answer", "data": final_answer}

    if isinstance(decoded, dict):
        return {"type": "final_answer", "data": decoded}

    return {"type": "invalid", "error": "no final_answer payload found"}


async def _execute_tool(
    tool_map: dict[str, dict[str, Any]],
    tool_name: str,
    params: dict[str, Any],
    bridge_url: str,
) -> dict[str, Any]:
    tool = tool_map.get(tool_name)
    if tool is None:
        return {"error": f"Tool '{tool_name}' is not available to this agent"}

    payload = {
        "project": tool.get("project") or "",
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
        response_body = await asyncio.to_thread(_read_http_response, request)
        decoded = json.loads(response_body)
    except Exception as exc:
        return {"error": str(exc)}

    if isinstance(decoded, dict) and "result" in decoded and len(decoded) == 1:
        result = decoded["result"]
        return result if isinstance(result, dict) else {"result": result}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


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
