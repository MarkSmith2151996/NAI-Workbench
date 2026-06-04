from __future__ import annotations

import os

import requests


LAPTOP_URL = os.environ.get("LAPTOP_BRIDGE_URL", "http://100.82.234.100:8222")
LAPTOP_TOKEN = os.environ.get("LAPTOP_BRIDGE_TOKEN", "")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if LAPTOP_TOKEN:
        headers["Authorization"] = f"Bearer {LAPTOP_TOKEN}"
    return headers


def _call_tool(tool_name: str, arguments: dict) -> object:
    try:
        response = requests.post(
            f"{LAPTOP_URL}/tool",
            json={"tool": tool_name, "arguments": arguments},
            headers=_headers(),
            timeout=130,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("result", data)
        return {"error": f"Bridge returned {response.status_code}: {response.text[:500]}"}
    except requests.ConnectionError:
        return {"error": "Cannot reach laptop bridge. Is the laptop on and connected via Tailscale?"}
    except requests.Timeout:
        return {"error": "Laptop bridge request timed out (130s)."}
    except Exception as exc:
        return {"error": f"Laptop bridge error: {exc}"}


def run_command(command: str, cwd: str | None = None, timeout: int = 120) -> object:
    return _call_tool("laptop_run_command", {"command": command, "cwd": cwd, "timeout": timeout})


def read_file(path: str, offset: int = 1, limit: int = 2000) -> object:
    return _call_tool("laptop_read_file", {"path": path, "offset": offset, "limit": limit})


def write_file(path: str, content: str) -> object:
    return _call_tool("laptop_write_file", {"path": path, "content": content})


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> object:
    return _call_tool(
        "laptop_edit_file",
        {"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all},
    )


def glob(pattern: str, path: str | None = None) -> object:
    payload = {"pattern": pattern}
    if path:
        payload["path"] = path
    return _call_tool("laptop_glob", payload)


def grep(pattern: str, path: str | None = None, glob_filter: str | None = None, context: int = 0, max_results: int = 50) -> object:
    payload = {"pattern": pattern, "context": context, "max_results": max_results}
    if path:
        payload["path"] = path
    if glob_filter:
        payload["glob_filter"] = glob_filter
    return _call_tool("laptop_grep", payload)


def list_dir(path: str | None = None) -> object:
    payload = {}
    if path:
        payload["path"] = path
    return _call_tool("laptop_list_dir", payload)


def system_info() -> object:
    return _call_tool("laptop_system_info", {})


def download_file(remote_path: str, local_path: str) -> object:
    headers = {}
    if LAPTOP_TOKEN:
        headers["Authorization"] = f"Bearer {LAPTOP_TOKEN}"
    try:
        response = requests.get(
            f"{LAPTOP_URL}/download",
            params={"path": remote_path},
            headers=headers,
            timeout=60,
            stream=True,
        )
        if response.status_code != 200:
            return {"error": f"Download failed: {response.status_code} {response.text[:500]}"}
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                handle.write(chunk)
        return {"remote_path": remote_path, "local_path": local_path, "size": os.path.getsize(local_path)}
    except requests.ConnectionError:
        return {"error": "Cannot reach laptop bridge. Is the laptop on and connected via Tailscale?"}
    except requests.Timeout:
        return {"error": "Download timed out (60s)."}
    except Exception as exc:
        return {"error": f"Download error: {exc}"}
