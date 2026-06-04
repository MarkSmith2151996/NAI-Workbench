#!/usr/bin/env python3
"""Shared OpenCode execution helper for Custodian.

System prompt handling uses pattern B: when a system prompt is provided, it is
prepended to the user message as:

## System
<system prompt>

## Task
<user message>

OpenCode does not expose a turn-limit flag. The helper accepts ``max_turns`` to
match existing Custodian APIs, but treats it as advisory only and logs a DEBUG
message when a non-default value is provided.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass

OPENCODE_BIN = os.environ.get(
    "NAI_WORKBENCH_OPENCODE_BIN", os.path.expanduser("~/.opencode/bin/opencode")
)

_MODEL_CACHE: dict[str, object] = {"timestamp": 0.0, "models": []}


@dataclass
class OpenCodeResult:
    text: str
    tokens_used: int
    cost_usd: float | None
    session_id: str | None
    exit_code: int
    stderr: str


class OpenCodeRunnerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        exit_code: int | None = None,
        text: str = "",
        tokens_used: int = 0,
        cost_usd: float | None = None,
        session_id: str | None = None,
    ):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code
        self.text = text
        self.tokens_used = tokens_used
        self.cost_usd = cost_usd
        self.session_id = session_id


def list_available_models() -> list[str]:
    """Return available OpenAI models from OpenCode, cached briefly."""
    now = time.time()
    cached_models = _MODEL_CACHE.get("models")
    if cached_models and now - float(_MODEL_CACHE.get("timestamp", 0.0)) < 60:
        return list(cached_models)

    try:
        result = subprocess.run(
            [OPENCODE_BIN, "models", "openai"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        raise OpenCodeRunnerError(f"failed to list models: {exc}") from exc

    if result.returncode != 0:
        raise OpenCodeRunnerError(
            "failed to list models",
            stderr=(result.stderr or result.stdout or "").strip(),
            exit_code=result.returncode,
        )

    models = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("openai/")]
    if not models:
        raise OpenCodeRunnerError("OpenCode returned no available OpenAI models")

    _MODEL_CACHE["timestamp"] = now
    _MODEL_CACHE["models"] = models
    return list(models)


def _build_input(prompt: str, system_prompt: str | None) -> str:
    user_message = prompt or ""
    if system_prompt:
        return f"## System\n{system_prompt}\n\n## Task\n{user_message}"
    return user_message


def run_opencode(
    prompt: str,
    model: str,
    system_prompt: str | None = None,
    project_dir: str | None = None,
    max_turns: int | None = None,
    timeout: int = 600,
) -> OpenCodeResult:
    """Run a one-shot OpenCode prompt and return text/tokens/cost metadata."""
    if max_turns not in (None, 20):
        logging.getLogger(__name__).debug(
            "OpenCode runner received advisory max_turns=%s but opencode run has no turn-limit flag.",
            max_turns,
        )

    env = os.environ.copy()
    env.setdefault("OPENCODE_DISABLE_UPDATE_CHECK", "1")

    cmd = [OPENCODE_BIN, "run", "--model", model, "--format", "json"]
    cwd = project_dir or os.getcwd()
    if project_dir:
        cmd.extend(["--dir", project_dir])

    input_text = _build_input(prompt, system_prompt)
    text_parts: list[str] = []
    tokens_used = 0
    cost_usd = None
    session_id = None
    parsed_events = 0

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise OpenCodeRunnerError(f"OpenCode CLI not found at {OPENCODE_BIN}") from exc
    except Exception as exc:
        raise OpenCodeRunnerError(f"failed to start OpenCode: {exc}") from exc

    try:
        assert proc.stdin is not None
        proc.stdin.write(input_text)
        proc.stdin.close()

        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            parsed_events += 1
            session_id = session_id or event.get("sessionID")

            if event.get("type") == "text":
                text_parts.append(event.get("part", {}).get("text", ""))
            elif event.get("type") == "step_finish":
                part = event.get("part", {})
                tokens_used = part.get("tokens", {}).get("total", tokens_used)
                cost_usd = part.get("cost", cost_usd)

        proc.wait(timeout=timeout)
        stderr_text = proc.stderr.read().strip() if proc.stderr is not None else ""
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stderr_text = proc.stderr.read().strip() if proc.stderr is not None else ""
        raise OpenCodeRunnerError(
            f"OpenCode timed out after {timeout}s",
            stderr=stderr_text,
            exit_code=None,
            text="".join(text_parts),
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            session_id=session_id,
        ) from exc
    except Exception as exc:
        proc.kill()
        stderr_text = proc.stderr.read().strip() if proc.stderr is not None else ""
        raise OpenCodeRunnerError(
            f"OpenCode run failed: {exc}",
            stderr=stderr_text,
            exit_code=proc.returncode,
            text="".join(text_parts),
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            session_id=session_id,
        ) from exc

    full_text = "".join(text_parts)
    if proc.returncode != 0:
        raise OpenCodeRunnerError(
            "OpenCode exited with a non-zero status",
            stderr=stderr_text,
            exit_code=proc.returncode,
            text=full_text,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            session_id=session_id,
        )
    if parsed_events == 0 or not full_text:
        raise OpenCodeRunnerError(
            "OpenCode produced no parseable text output",
            stderr=stderr_text,
            exit_code=proc.returncode,
            text=full_text,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            session_id=session_id,
        )

    return OpenCodeResult(
        text=full_text,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        session_id=session_id,
        exit_code=proc.returncode,
        stderr=stderr_text,
    )
