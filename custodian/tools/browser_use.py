from __future__ import annotations

import asyncio
import json
from mcp.types import TextContent
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

_CDP_CONTEXT_LOCK = asyncio.Lock()
STEEL_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
KEEPA_COOKIE_FILE = "/mnt/c/Users/Big A/custodian-shared/nai-workbench/steel-browser-setup/keepa-cookies.json"

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
            },
            "use_steel": {
                "type": "boolean",
                "description": "Use local Steel Browser for a fresh managed browser session. Default true. Set false to use the legacy local Chromium or chrome_cdp mode.",
                "default": True
            },
            "steel_profile": {
                "type": "string",
                "description": "Optional Steel profile ID to reuse persisted auth state, e.g. 'keepa-auth'."
            },
            "steel_url": {
                "type": "string",
                "description": "Steel Browser API base URL. Defaults to STEEL_BASE_URL or http://localhost:3010 in this WSL setup."
            },
            "steel_session_url": {
                "type": "string",
                "description": "WebSocket URL of an existing Steel session to reuse. If provided, connects to this session and creates a new page instead of creating a new session. The session must already be authenticated.",
                "default": ""
            },
            "isolated_context": {
                "type": "boolean",
                "description": "Legacy chrome_cdp fallback only. If true (default when chrome_cdp is set and use_steel=false), creates an isolated browser context per call. Steel handles isolation per session automatically."
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


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _normalize_steel_ws_url(ws_url: str, steel_url: str) -> str:
    from urllib.parse import urlparse

    parsed_steel = urlparse(steel_url)
    if not parsed_steel.scheme or not parsed_steel.netloc:
        raise ValueError(f"invalid steel_url: {steel_url}")

    ws_scheme = "wss" if parsed_steel.scheme == "https" else "ws"
    if not ws_url:
        return f"{ws_scheme}://{parsed_steel.netloc}/"

    parsed_ws = urlparse(ws_url)
    if parsed_ws.hostname in {"0.0.0.0", "127.0.0.1", "localhost"}:
        path = parsed_ws.path or "/"
        query = f"?{parsed_ws.query}" if parsed_ws.query else ""
        return f"{ws_scheme}://{parsed_steel.netloc}{path}{query}"
    return ws_url


def _steel_request(method: str, steel_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = steel_url.rstrip("/")
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Steel API {method} {path} failed with HTTP {exc.code}: {body}") from exc


def _create_steel_session(steel_url: str, steel_profile: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"userAgent": STEEL_USER_AGENT}
    if steel_profile:
        payload["profileId"] = steel_profile
    return _steel_request("POST", steel_url, "/v1/sessions", payload)


def _release_steel_session(steel_url: str, session_id: str) -> None:
    _steel_request("POST", steel_url, f"/v1/sessions/{session_id}/release", {})


def _is_keepa_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return domain == "keepa.com" or domain.endswith(".keepa.com")


def _to_cookie_param(cookie: dict[str, Any]) -> dict[str, Any]:
    param: dict[str, Any] = {
        "name": cookie.get("name"),
        "value": cookie.get("value", ""),
    }
    for key in ("domain", "path", "secure", "httpOnly", "sameSite", "priority", "sameParty", "sourceScheme", "sourcePort"):
        if cookie.get(key) is not None:
            param[key] = cookie[key]
    if cookie.get("expires") and cookie.get("expires") != -1:
        param["expires"] = cookie["expires"]
    return param


def _load_keepa_cookie_params(cookie_file: str = KEEPA_COOKIE_FILE) -> list[dict[str, Any]]:
    if not os.path.exists(cookie_file):
        return []

    with open(cookie_file, encoding="utf-8") as f:
        raw_cookies = json.load(f)

    if isinstance(raw_cookies, dict):
        raw_cookies = raw_cookies.get("cookies", [])
    if not isinstance(raw_cookies, list):
        return []

    cookie_params = []
    for cookie in raw_cookies:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        param: dict[str, Any] = {
            "name": cookie["name"],
            "value": cookie.get("value", ""),
            "domain": cookie.get("domain", "keepa.com"),
            "path": cookie.get("path", "/"),
        }
        if cookie.get("secure"):
            param["secure"] = True
        if cookie.get("httpOnly"):
            param["httpOnly"] = True
        if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
            param["sameSite"] = cookie["sameSite"]
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if expires and expires != -1:
            param["expires"] = expires
        cookie_params.append(param)
    return cookie_params


def _build_steel_session_class(base_session_cls, inject_keepa_cookies: bool = True, track_pages: bool = False):
    class SteelBrowserSession(base_session_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            object.__setattr__(self, "_keepa_cookie_injection_count", 0)
            object.__setattr__(self, "_keepa_storage_injection_count", 0)
            object.__setattr__(self, "_steel_created_target_ids", set())
            object.__setattr__(self, "_steel_shared_page_initialized", False)

        async def start(self) -> None:
            await super().start()
            if track_pages and not self._steel_shared_page_initialized:
                page = await self.new_page("about:blank")
                target_id = getattr(page, "_target_id", None)
                if target_id:
                    await self.get_or_create_cdp_session(target_id, focus=True)
                object.__setattr__(self, "_steel_shared_page_initialized", True)

        async def connect(self, cdp_url: str | None = None):
            await super().connect(cdp_url=cdp_url)
            if inject_keepa_cookies:
                await self._inject_keepa_cookies_from_file()
            return self

        async def new_page(self, url: str | None = None):
            page = await super().new_page(url=url)
            if track_pages:
                target_id = getattr(page, "_target_id", None)
                if target_id:
                    self._steel_created_target_ids.add(target_id)
            return page

        async def close_steel_pages(self) -> None:
            client = getattr(self, "_cdp_client_root", None) or getattr(self, "cdp_client", None)
            if client is None:
                return
            for target_id in list(self._steel_created_target_ids):
                try:
                    await self.close_page(target_id)
                except Exception:
                    pass
                finally:
                    self._steel_created_target_ids.discard(target_id)

        async def _inject_keepa_cookies_from_file(self) -> None:
            try:
                keepa_cookies = _load_keepa_cookie_params()
                object.__setattr__(self, "_keepa_cookie_injection_count", len(keepa_cookies))

                if not keepa_cookies:
                    self.logger.warning(f"No Keepa cookies loaded from {KEEPA_COOKIE_FILE} - agent may hit login wall")
                    return

                await self.cdp_client.send.Storage.setCookies(params={"cookies": keepa_cookies})
                self.logger.info(f"Injected {len(keepa_cookies)} Keepa cookies from {KEEPA_COOKIE_FILE} into Steel session")
            except Exception as exc:
                object.__setattr__(self, "_keepa_cookie_injection_count", 0)
                self.logger.warning(f"Failed to inject Keepa cookies from {KEEPA_COOKIE_FILE}: {exc}")

    return SteelBrowserSession


def _build_isolated_cdp_session_class(base_session_cls):
    class IsolatedCDPBrowserSession(base_session_cls):
        """BrowserSession variant that keeps CDP-created targets inside one browser context."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            object.__setattr__(self, "_isolated_browser_context_id", None)
            object.__setattr__(self, "_isolated_target_ids", set())
            object.__setattr__(self, "_isolated_warmup_target_ids", set())
            object.__setattr__(self, "_keepa_cookie_injection_count", 0)
            object.__setattr__(self, "_keepa_storage_injection_count", 0)
            object.__setattr__(self, "_keepa_storage_seeded", False)

        async def start(self) -> None:
            await super().start()
            if self._isolated_target_ids:
                target_id = next(iter(self._isolated_target_ids))
                if not self._keepa_storage_seeded and self._isolated_warmup_target_ids:
                    keepa_storage = await self._read_keepa_origin_storage(next(iter(self._isolated_warmup_target_ids)))
                    await self._inject_keepa_origin_storage(target_id, keepa_storage)
                    object.__setattr__(self, "_keepa_storage_seeded", True)
                self.agent_focus_target_id = target_id

        async def connect(self, cdp_url: str | None = None):
            await super().connect(cdp_url=cdp_url)
            if self._isolated_browser_context_id is None:
                async with _CDP_CONTEXT_LOCK:
                    # Chrome can reject browser-context targets if the remote profile has no
                    # normal tab open yet; creating a default target first stabilizes CDP.
                    warmup = await self.cdp_client.send.Target.createTarget(params={"url": "about:blank"})
                    if warmup.get("targetId"):
                        self._isolated_warmup_target_ids.add(warmup["targetId"])
                    result = await self.cdp_client.send.Target.createBrowserContext(params={})
                    object.__setattr__(self, "_isolated_browser_context_id", result["browserContextId"])
                    await self._inject_keepa_cookies(result["browserContextId"])
                    target_id = await self._cdp_create_new_page("https://keepa.com/")
                    await self.get_or_create_cdp_session(target_id, focus=True)
                    self.agent_focus_target_id = target_id
            return self

        async def _read_keepa_origin_storage(self, target_id: str) -> dict[str, str]:
            try:
                attached = await self.cdp_client.send.Target.attachToTarget(params={"targetId": target_id, "flatten": True})
                session_id = attached["sessionId"]
                try:
                    await self.cdp_client.send.Page.enable(session_id=session_id)
                    await self.cdp_client.send.Runtime.enable(session_id=session_id)
                    await self.cdp_client.send.Page.navigate(params={"url": "https://keepa.com/"}, session_id=session_id)
                    await asyncio.sleep(8)
                    result = await self.cdp_client.send.Runtime.evaluate(
                        params={
                            "expression": "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))",
                            "returnByValue": True,
                        },
                        session_id=session_id,
                    )
                    raw = result.get("result", {}).get("value") or "{}"
                    data = json.loads(raw)
                    return data if isinstance(data, dict) else {}
                finally:
                    try:
                        await self.cdp_client.send.Target.detachFromTarget(params={"sessionId": session_id})
                    except Exception:
                        pass
            except Exception as exc:
                self.logger.warning(f"Failed to read Keepa localStorage from default context: {exc}")
                return {}

        async def _inject_keepa_origin_storage(self, target_id: str, storage: dict[str, str]) -> None:
            if not storage:
                self.logger.warning("No Keepa localStorage found in default context - agent may hit login wall")
                return
            try:
                attached = await self.cdp_client.send.Target.attachToTarget(params={"targetId": target_id, "flatten": True})
                session_id = attached["sessionId"]
                try:
                    await self.cdp_client.send.Page.enable(session_id=session_id)
                    await self.cdp_client.send.Runtime.enable(session_id=session_id)
                    expression = "const data = " + json.dumps(storage) + "; Object.entries(data).forEach(([k, v]) => localStorage.setItem(k, v)); true;"
                    await self.cdp_client.send.Runtime.evaluate(
                        params={"expression": expression, "returnByValue": True}, session_id=session_id
                    )
                    await self.cdp_client.send.Page.reload(params={"ignoreCache": True}, session_id=session_id)
                    await asyncio.sleep(8)
                finally:
                    try:
                        await self.cdp_client.send.Target.detachFromTarget(params={"sessionId": session_id})
                    except Exception:
                        pass
                object.__setattr__(self, "_keepa_storage_injection_count", len(storage))
                self.logger.info(f"Injected {len(storage)} Keepa localStorage keys into isolated context")
            except Exception as exc:
                object.__setattr__(self, "_keepa_storage_injection_count", 0)
                self.logger.warning(f"Failed to inject Keepa localStorage into isolated context: {exc}")

        async def _inject_keepa_cookies(self, context_id: str) -> None:
            try:
                result = await self.cdp_client.send.Storage.getCookies(params={})
                keepa_cookies = [_to_cookie_param(cookie) for cookie in result.get("cookies", []) if _is_keepa_cookie(cookie)]
                keepa_cookies = [cookie for cookie in keepa_cookies if cookie.get("name")]
                object.__setattr__(self, "_keepa_cookie_injection_count", len(keepa_cookies))

                if not keepa_cookies:
                    self.logger.warning("No Keepa cookies found in default context - agent may hit login wall")
                    return

                await self.cdp_client.send.Storage.setCookies(
                    params={"cookies": keepa_cookies, "browserContextId": context_id}
                )
                self.logger.info(
                    f"Injected {len(keepa_cookies)} cookies from default context into isolated context {context_id}"
                )
            except Exception as exc:
                object.__setattr__(self, "_keepa_cookie_injection_count", 0)
                self.logger.warning(f"Failed to inject Keepa cookies into isolated context {context_id}: {exc}")

        async def _isolated_context_target_ids(self) -> set[str]:
            context_id = self._isolated_browser_context_id
            if not context_id or self._cdp_client_root is None:
                return set()

            result = await self.cdp_client.send.Target.getTargets()
            target_ids = {
                target["targetId"]
                for target in result.get("targetInfos", [])
                if target.get("browserContextId") == context_id
            }
            self._isolated_target_ids.update(target_ids)
            return set(self._isolated_target_ids)

        async def _cdp_create_new_page(self, url: str = "about:blank", background: bool = False, new_window: bool = False) -> str:
            params = {"url": url, "newWindow": new_window, "background": background}
            if self._isolated_browser_context_id:
                params["browserContextId"] = self._isolated_browser_context_id

            client = self._cdp_client_root or self.cdp_client
            try:
                result = await client.send.Target.createTarget(params=params)
            except Exception as exc:
                if "Failed to open new tab - no browser is open" not in str(exc) or not self._isolated_browser_context_id:
                    raise

                warmup = await client.send.Target.createTarget(params={"url": "about:blank"})
                if warmup.get("targetId"):
                    self._isolated_warmup_target_ids.add(warmup["targetId"])
                retry_params = {**params, "newWindow": True}
                result = await client.send.Target.createTarget(params=retry_params)
            target_id = result["targetId"]
            self._isolated_target_ids.add(target_id)
            return target_id

        async def new_page(self, url: str | None = None):
            target_id = await self._cdp_create_new_page(url or "about:blank")
            from browser_use.actor.page import Page as Target

            return Target(self, target_id)

        def get_page_targets(self):
            target_ids = set(self._isolated_target_ids)
            return [target for target in super().get_page_targets() if target.target_id in target_ids]

        async def _cdp_get_all_pages(self, *args, **kwargs):
            target_ids = await self._isolated_context_target_ids()
            pages = await super()._cdp_get_all_pages(*args, **kwargs)
            return [page for page in pages if page.get("targetId") in target_ids]

        async def close_isolated_context(self) -> None:
            context_id = self._isolated_browser_context_id
            if not context_id or self._cdp_client_root is None:
                return
            try:
                self.agent_focus_target_id = None
                await self._cdp_client_root.send.Target.disposeBrowserContext(params={"browserContextId": context_id})
                for target_id in list(self._isolated_warmup_target_ids):
                    try:
                        await self._cdp_client_root.send.Target.closeTarget(params={"targetId": target_id})
                    except Exception:
                        pass
            finally:
                object.__setattr__(self, "_isolated_browser_context_id", None)
                self._isolated_target_ids.clear()
                self._isolated_warmup_target_ids.clear()

    return IsolatedCDPBrowserSession


async def handle(params: dict, db):
    task = (params.get("task") or "").strip()
    if not task:
        return {"success": False, "error": "task is required", "result": "", "downloaded_files": [], "steps_taken": 0}
    
    start_url = (params.get("start_url") or "").strip()
    user_data_dir = (params.get("user_data_dir") or "").strip() or None
    chrome_cdp = (params.get("chrome_cdp") or "").strip() or None
    use_steel = _coerce_bool(params.get("use_steel"), default=True)
    steel_profile = (params.get("steel_profile") or "").strip() or None
    steel_url = (params.get("steel_url") or os.environ.get("STEEL_BASE_URL") or "http://localhost:3010").strip().rstrip("/")
    steel_session_url = (params.get("steel_session_url") or "").strip()
    isolated_context = _coerce_bool(params.get("isolated_context"), default=bool(chrome_cdp and not use_steel))
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
    steel_session: dict[str, Any] | None = None
    steel_cdp_url = None
    owns_steel_session = False
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
        if use_steel:
            if steel_session_url:
                steel_cdp_url = _normalize_steel_ws_url(steel_session_url, steel_url)
            else:
                steel_session = _create_steel_session(steel_url, steel_profile)
                steel_cdp_url = _normalize_steel_ws_url(str(steel_session.get("websocketUrl") or ""), steel_url)
                owns_steel_session = True
            browser_session = _build_steel_session_class(
                BrowserSession,
                inject_keepa_cookies=owns_steel_session,
                track_pages=bool(steel_session_url),
            )(
                cdp_url=steel_cdp_url,
                downloads_path=str(output_dir),
                accept_downloads=True,
            )
        elif chrome_cdp:
            session_cls = _build_isolated_cdp_session_class(BrowserSession) if isolated_context else BrowserSession
            browser_session = session_cls(
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
            "use_steel": use_steel,
            "steel_shared_session": bool(steel_session_url),
            "steel_session_id": steel_session.get("id") if steel_session else None,
            "steel_url": steel_url if use_steel else None,
            "steel_cdp_url": steel_cdp_url if use_steel else None,
            "isolated_context": bool((not use_steel) and chrome_cdp and isolated_context),
            "keepa_cookies_injected": int(getattr(browser_session, "_keepa_cookie_injection_count", 0) or 0),
            "keepa_storage_keys_injected": int(getattr(browser_session, "_keepa_storage_injection_count", 0) or 0),
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
                if use_steel and steel_session_url and hasattr(browser_session, "close_steel_pages"):
                    await browser_session.close_steel_pages()
                elif (not use_steel) and chrome_cdp and isolated_context and hasattr(browser_session, "close_isolated_context"):
                    await browser_session.close_isolated_context()
                await browser_session.stop()
            except Exception:
                pass
        if owns_steel_session and steel_session is not None and steel_session.get("id"):
            try:
                _release_steel_session(steel_url, str(steel_session["id"]))
            except Exception:
                pass
