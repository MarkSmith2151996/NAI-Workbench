from __future__ import annotations

import json
from mcp.types import TextContent
import os
from pathlib import Path
from typing import Any

METADATA = {
    "name": "browser_use",
    "description": "Run a browser automation task using natural language. Uses browser-use (Playwright + LLM) to navigate pages, click elements, fill forms, extract data, and download files. Supports headless Chromium on WSL \u2014 no cross-machine networking required.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Natural language description of what to do in the browser. Be specific about what to navigate to, what to interact with, and what data to return."
            },
            "start_url": {
                "type": "string",
                "description": "Optional starting URL. If omitted, the agent navigates from about:blank."
            },
            "output_dir": {
                "type": "string",
                "description": "Directory for file downloads. Defaults to /mnt/c/Users/Big A/custodian-shared/nai-workbench/browser-use-outputs/"
            },
            "max_steps": {
                "type": "integer",
                "description": "Maximum number of browser actions before stopping. Default 15.",
                "default": 15
            },
            "user_data_dir": {
                "type": "string",
                "description": "Path to persistent Chromium profile directory. If provided, cookies and login sessions persist across calls. If omitted, each call gets an ephemeral session."
            },
            "chrome_cdp": {
                "type": "string",
                "description": "CDP endpoint URL to connect to an existing Chrome instance (e.g. 'http://100.95.20.98:9222'). If provided, connects via CDP instead of launching headless Chromium. The existing Chrome's cookies, login sessions, and Cloudflare clearance are inherited."
            }
        },
        "required": [
            "task"
        ]
    }
}


def _truncate(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _build_action_log(history) -> list[dict[str, Any]]:
    action_log = []
    try:
        action_history = history.action_history()
    except Exception as exc:
        return [{"step": None, "error": f"failed to extract action history: {type(exc).__name__}: {exc}"}]

    for step_number, step_actions in enumerate(action_history, start=1):
        actions = []
        for action in step_actions:
            if not isinstance(action, dict):
                actions.append({"action": _truncate(action)})
                continue

            action_data = {key: value for key, value in action.items() if key not in {"result", "interacted_element"}}
            actions.append(
                {
                    "action": _truncate(action_data, 500),
                    "result": _truncate(action.get("result"), 200),
                    "interacted_element": _truncate(action.get("interacted_element"), 500),
                }
            )
        action_log.append({"step": step_number, "actions": actions})
    return action_log


async def handle(params: dict, db):
    task = (params.get("task") or "").strip()
    if not task:
        return {"success": False, "error": "task is required", "result": "", "downloaded_files": [], "steps_taken": 0}
    
    start_url = (params.get("start_url") or "").strip()
    user_data_dir = (params.get("user_data_dir") or "").strip() or None
    chrome_cdp = (params.get("chrome_cdp") or "").strip() or None
    default_output_dir = "/mnt/c/Users/Big A/custodian-shared/nai-workbench/browser-use-outputs"
    output_dir = Path(params.get("output_dir") or default_output_dir).expanduser()
    
    try:
        output_dir = output_dir.resolve()
    except Exception:
        return {"success": False, "error": f"invalid output_dir: {output_dir}", "result": "", "downloaded_files": [], "steps_taken": 0}
    
    shared_root = Path("/mnt/c/Users/Big A/custodian-shared").resolve()
    try:
        is_shared_path = output_dir == shared_root or shared_root in output_dir.parents
    except Exception:
        is_shared_path = False
    
    if is_shared_path and not output_dir.is_dir():
        return {
            "success": False,
            "error": f"shared output_dir does not exist: {output_dir}",
            "result": "",
            "downloaded_files": [],
            "steps_taken": 0,
        }
    
    if not is_shared_path:
        output_dir.mkdir(parents=True, exist_ok=True)
    elif not os.access(output_dir, os.W_OK):
        return {
            "success": False,
            "error": f"output_dir is not writable: {output_dir}",
            "result": "",
            "downloaded_files": [],
            "steps_taken": 0,
        }
    
    try:
        max_steps = int(params.get("max_steps") or 15)
    except (TypeError, ValueError):
        max_steps = 15
    max_steps = max(1, min(max_steps, 100))
    
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:4096/v1"
    os.environ["OPENAI_API_KEY"] = "sk-placeholder-proxy-handles-auth"
    
    before_files = {str(path) for path in output_dir.glob("**/*") if path.is_file()}
    browser_session = None
    try:
        from browser_use import Agent, BrowserSession
        from browser_use.llm.openai.chat import ChatOpenAI
    
        llm = ChatOpenAI(
            model="gpt-5.4",
            base_url="http://127.0.0.1:4096/v1",
            api_key="sk-placeholder-proxy-handles-auth",
            temperature=None,
            reasoning_effort="low",
        )
        if chrome_cdp:
            browser_session = BrowserSession(
                cdp_url=chrome_cdp,
                downloads_path=str(output_dir),
                accept_downloads=True,
            )
        else:
            browser_session = BrowserSession(
                headless=True,
                downloads_path=str(output_dir),
                accept_downloads=True,
                chromium_sandbox=False,
                user_data_dir=user_data_dir,
            )
        full_task = task if not start_url else f"Start at {start_url}. {task}"
        agent = Agent(
            task=full_task,
            llm=llm,
            browser_session=browser_session,
            max_failures=3,
            use_vision=False,
        )
        history = await agent.run(max_steps=max_steps)
        after_files = {str(path) for path in output_dir.glob("**/*") if path.is_file()}
        downloaded_files = sorted(after_files - before_files)
    
        return {
            "success": bool(history.is_successful()),
            "result": history.final_result() or "",
            "downloaded_files": downloaded_files,
            "steps_taken": history.number_of_steps(),
            "action_log": _build_action_log(history),
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"browser-use task failed: {type(exc).__name__}: {exc}",
            "result": "",
            "downloaded_files": [],
            "steps_taken": 0,
        }
    finally:
        if browser_session is not None:
            try:
                await browser_session.stop()
            except Exception:
                pass
