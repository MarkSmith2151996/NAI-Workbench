from __future__ import annotations

import os
import threading

import requests


PENPOT_BASE = os.environ.get("PENPOT_URL", "http://localhost:9001")
PENPOT_EMAIL = os.environ.get("PENPOT_EMAIL", "admin@local.dev")
PENPOT_PASSWORD = os.environ.get("PENPOT_PASSWORD", "admin123")

_session_lock = threading.Lock()
_session: requests.Session | None = None


def _rpc(command: str, payload: dict | None = None, retry: bool = False) -> object:
    global _session
    with _session_lock:
        if _session is None:
            session = requests.Session()
            response = session.post(
                f"{PENPOT_BASE}/api/rpc/command/login-with-password",
                json={"email": PENPOT_EMAIL, "password": PENPOT_PASSWORD},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=10,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Penpot login failed ({response.status_code}): {response.text[:200]}")
            token = response.cookies.get("auth-token")
            if token:
                session.headers["Authorization"] = f"Bearer {token}"
            _session = session
        session = _session

    response = session.post(
        f"{PENPOT_BASE}/api/rpc/command/{command}",
        json=payload or {},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if response.status_code != 200:
        if retry:
            raise RuntimeError(f"Penpot RPC '{command}' failed ({response.status_code}): {response.text[:200]}")
        with _session_lock:
            if _session is not None:
                try:
                    _session.close()
                except Exception:
                    pass
                _session = None
        return _rpc(command, payload, retry=True)
    return response.json()


def _extract_shape_info(shape: dict) -> dict:
    info = {"name": shape.get("name", ""), "type": shape.get("type", "")}
    content = shape.get("content")
    if content and isinstance(content, dict):
        texts: list[str] = []
        for para in content.get("children", []):
            for span in para.get("children", []):
                if "text" in span:
                    texts.append(span["text"])
        if texts:
            info["text"] = " ".join(texts)
    return info


def list_projects() -> list[dict]:
    projects_data = _rpc("get-all-projects")
    results = []
    for project in projects_data:
        project_id = project["id"]
        files = _rpc("get-project-files", {"project-id": project_id})
        results.append(
            {
                "id": project_id,
                "name": project.get("name", ""),
                "files": [{"id": f["id"], "name": f.get("name", ""), "modified": f.get("modified-at", "")} for f in files],
            }
        )
    return results


def get_page(file_id: str, page: str | None = None) -> list[dict] | str:
    file_data = _rpc("get-file", {"id": file_id})
    pages_index = file_data.get("data", {}).get("pages-index", {})
    results = []
    for page_id, page_obj in pages_index.items():
        name = page_obj.get("name", "")
        if page and page.lower() != name.lower():
            continue
        objects = page_obj.get("objects", {})
        shapes = []
        for shape in objects.values():
            info = _extract_shape_info(shape)
            if info["name"] or info.get("text"):
                shapes.append(info)
        results.append({"page_id": page_id, "name": name, "shape_count": len(objects), "components": shapes[:100]})
    return results or f"No pages found{' matching ' + repr(page) if page else ''}."


def export_svg(file_id: str, page: str | None = None) -> str:
    file_data = _rpc("get-file", {"id": file_id})
    pages_index = file_data.get("data", {}).get("pages-index", {})
    target_page = None
    for page_obj in pages_index.values():
        name = page_obj.get("name", "")
        if page:
            if page.lower() == name.lower():
                target_page = page_obj
                break
        else:
            target_page = page_obj
            break
    if target_page is None:
        return f"Page not found{' matching ' + repr(page) if page else ''}."

    objects = target_page.get("objects", {})
    root = objects.get("00000000-0000-0000-0000-000000000000", {})
    width = root.get("width", 1920)
    height = root.get("height", 1080)
    svg_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
    ]
    for shape in objects.values():
        stype = shape.get("type", "")
        name = shape.get("name", "")
        x = shape.get("x", 0)
        y = shape.get("y", 0)
        w = shape.get("width", 0)
        h = shape.get("height", 0)
        if stype == "frame":
            svg_parts.append(f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" stroke="#999" data-name="{name}"/>')
        elif stype == "rect":
            fill = "#ccc"
            fills = shape.get("fills", [])
            if fills and isinstance(fills, list) and fills[0].get("color"):
                fill = fills[0]["color"]
            svg_parts.append(f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" data-name="{name}"/>')
        elif stype == "text":
            info = _extract_shape_info(shape)
            text = info.get("text", name)
            svg_parts.append(f'  <text x="{x}" y="{y + 16}" font-size="14" data-name="{name}">{text}</text>')
    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
