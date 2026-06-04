from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import os
import platform as _platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import quote

from custodian.db.connection import DB_PATH, db_connection
from mcp.types import TextContent

LIVE_FILE_TREE_ENTRY_LIMIT = 50
SHARED_FOLDER_ROOT = "/mnt/c/Users/Big A/custodian-shared"
SHARED_FOLDER_MAX_BYTES = 5 * 1024 * 1024
SHARED_FOLDER_BINARY_SNIFF_BYTES = 8192
SHARED_FOLDER_OVERSIZE_PREVIEW_LINES = 100
SHARED_FOLDER_CATEGORY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _check_wsl():
    if _platform.system() != "Linux" or not os.path.exists("/proc/version"):
        return False
    with open("/proc/version") as f:
        return "microsoft" in f.read().lower()


_IS_WSL = _check_wsl()
CUSTODIAN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _to_native_path(path):
    if not _IS_WSL or not path:
        return path
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def _find_symbol(*args, **kwargs):
    try:
        from custodian.parse_symbols import find_symbol
    except ImportError:
        from parse_symbols import find_symbol
    return find_symbol(*args, **kwargs)


def get_project_by_name(conn, name):
    row = conn.execute("SELECT * FROM projects WHERE name = ? AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM projects WHERE LOWER(name) = LOWER(?) AND status = 'active'", (name,)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM projects WHERE LOWER(name) LIKE LOWER(?) AND status = 'active'", (f"%{name}%",)).fetchone()

def _shared_project_root(project_name):
    return os.path.join(SHARED_FOLDER_ROOT, project_name)

def _ensure_shared_project_root(project_name):
    """Create a project's shared folder before Docker bind-mounts it."""
    path = _shared_project_root(project_name)
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return path

def _validate_shared_category(category):
    category = str(category or "").strip()
    if len(category) > 64 or not SHARED_FOLDER_CATEGORY_RE.fullmatch(category):
        raise ValueError(
            "'category' must be lowercase kebab-case, max 64 chars, matching "
            "^[a-z0-9]+(-[a-z0-9]+)*$."
        )
    return category

def _validate_shared_relative_path(relative_path):
    relative_path = str(relative_path or "").strip()
    if not relative_path:
        raise ValueError("'relative_path' is required.")
    if os.path.isabs(relative_path):
        raise ValueError("Absolute paths are not allowed.")
    path_parts = relative_path.replace("\\", "/").split("/")
    if ".." in path_parts:
        raise ValueError("Path escapes shared folder boundary.")
    return relative_path

def _read_text_file_window(path, offset=1, limit=2000):
    lines = []
    total_lines = 0
    start = max(int(offset or 1), 1)
    max_lines = max(int(limit or 2000), 0)

    with open(path, encoding="utf-8") as f:
        for total_lines, line in enumerate(f, start=1):
            if total_lines < start:
                continue
            if len(lines) < max_lines:
                lines.append(line)

    truncated = total_lines > (start - 1 + len(lines))
    return {
        "content": "".join(lines),
        "total_lines": total_lines,
        "lines_returned": len(lines),
        "offset": start,
        "truncated": truncated,
    }

def _read_text_file_preview(path, preview_lines):
    lines = []
    total_lines = 0

    with open(path, encoding="utf-8") as f:
        for total_lines, line in enumerate(f, start=1):
            if len(lines) < preview_lines:
                lines.append(line)

    return {
        "content": "".join(lines),
        "total_lines": total_lines,
        "lines_returned": len(lines),
        "offset": 1,
        "truncated": total_lines > len(lines),
    }

def _index_to_prefix(n: int) -> str:
    """0 -> AA, 1 -> BB, ... 25 -> ZZ, 26 -> AAA, ... 51 -> ZZZ."""
    if n < 0:
        raise ValueError("Task prefix index must be non-negative.")
    if n < 26:
        return chr(ord("A") + n) * 2
    if n < 52:
        return chr(ord("A") + (n - 26)) * 3
    raise ValueError("Task prefix space exhausted (supports AA-ZZ and AAA-ZZZ only).")

def _assign_missing_project_prefixes(conn):
    """Backfill prefixes and assign them to newly registered projects on restart."""
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE task_prefix IS NOT NULL"
    ).fetchone()[0]
    projects_without = conn.execute(
        "SELECT id, name FROM projects WHERE task_prefix IS NULL ORDER BY name"
    ).fetchall()

    for i, row in enumerate(projects_without):
        prefix = _index_to_prefix(existing_count + i)
        conn.execute(
            "UPDATE projects SET task_prefix = ? WHERE id = ?",
            (prefix, row["id"]),
        )
        print(
            f"[custodian] Assigned task prefix {prefix} to project {row['name']}",
            file=sys.stderr,
        )

def _parse_stored_json_array(value):
    """Return a stored JSON array or [] for null/empty/malformed values."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []

def _start_router_fallback():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", ROUTER_PORT))
        # Port already bound — standalone service is running
        return
    except (ConnectionRefusedError, OSError):
        pass
    try:
        from sandbox_router import run_router as _run_sandbox_router
        threading.Thread(target=_run_sandbox_router, daemon=True).start()
    except ImportError:
        pass

def _truncate_text(text, limit):
    """Trim large text fields for compact responses."""
    if not text:
        return text
    compact = re.sub(r"\s+", " ", str(text)).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."

def _safe_json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback

def _build_fossil_description_map(file_tree_value):
    """Build a relative-path -> description map from fossil file_tree JSON."""
    descriptions = {}
    file_tree = _safe_json_loads(file_tree_value, [])
    if not isinstance(file_tree, list):
        return descriptions

    for entry in file_tree:
        if not isinstance(entry, dict):
            continue
        rel_path = entry.get("path")
        description = entry.get("description")
        if isinstance(rel_path, str) and rel_path:
            descriptions[rel_path.replace("\\", "/")] = description
    return descriptions

def _stat_file_tree_entry(args):
    """Read file metadata for one live file tree entry."""
    full_path, rel_path, fossil_descriptions = args
    try:
        stat = os.stat(full_path)
    except OSError:
        return None

    item = {
        "path": rel_path,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }
    description = fossil_descriptions.get(rel_path)
    if description:
        item["description"] = description
    return item

def _path_allowed_for_live_tree(rel_path, excluded_dirs, max_depth):
    """Apply directory exclusions and depth limit to a relative file path."""
    normalized = rel_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts:
        return False
    if len(parts) > max_depth:
        return False
    for part in parts[:-1]:
        if part in excluded_dirs or part.endswith(".egg-info"):
            return False
    return True

def _build_live_file_tree(project_path, fossil_descriptions, max_entries=500, max_depth=8):
    """Walk the live project tree and return compact file metadata."""
    excluded_dirs = {
        "node_modules", ".git", "dist", ".next", "__pycache__",
        ".venv", "build", ".cache", "target", ".pytest_cache",
        ".idea", ".vscode", "coverage", "htmlcov",
    }
    entries = []
    pending_stats = []
    total_files = 0

    git_dir = os.path.join(project_path, ".git")
    if os.path.exists(git_dir):
        try:
            git_list = subprocess.run(
                ["git", "-C", project_path, "ls-files", "-co", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if git_list.returncode == 0:
                for raw_path in git_list.stdout.splitlines():
                    rel_path = raw_path.strip().replace("\\", "/")
                    if not rel_path or not _path_allowed_for_live_tree(rel_path, excluded_dirs, max_depth):
                        continue
                    total_files += 1
                    if len(pending_stats) < max_entries:
                        pending_stats.append((os.path.join(project_path, rel_path), rel_path, fossil_descriptions))

                if pending_stats or total_files == 0:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                        for item in executor.map(_stat_file_tree_entry, pending_stats):
                            if item is not None:
                                entries.append(item)
                    entries.sort(key=lambda item: item["path"])
                    return {
                        "entries": entries,
                        "total_files": total_files,
                        "truncated_at": max_entries if total_files > max_entries else None,
                    }
        except Exception:
            pass

    for root, dirs, files in os.walk(project_path):
        rel_root = os.path.relpath(root, project_path)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        dirs[:] = [
            d for d in dirs
            if d not in excluded_dirs and not d.endswith(".egg-info")
        ]
        if depth >= max_depth:
            dirs[:] = []

        for filename in files:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, project_path).replace("\\", "/")
            total_files += 1
            if len(pending_stats) >= max_entries:
                continue

            pending_stats.append((full_path, rel_path, fossil_descriptions))

    if pending_stats:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            for item in executor.map(_stat_file_tree_entry, pending_stats):
                if item is not None:
                    entries.append(item)

    entries.sort(key=lambda item: item["path"])
    return {
        "entries": entries,
        "total_files": total_files,
        "truncated_at": max_entries if total_files > max_entries else None,
    }

def _parse_git_status(stdout, max_entries=50):
    """Parse git status --porcelain into capped buckets."""
    status = {
        "modified": [],
        "added": [],
        "deleted": [],
        "untracked": [],
    }

    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        path = raw_line[3:].strip()
        if not path:
            continue

        if code == "??":
            bucket = "untracked"
        elif "D" in code:
            bucket = "deleted"
        elif "A" in code:
            bucket = "added"
        else:
            bucket = "modified"

        if len(status[bucket]) < max_entries:
            status[bucket].append(path)

    return status

def _parse_git_log(stdout):
    """Parse git log --oneline output into hash/message pairs."""
    commits = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        commits.append({
            "hash": parts[0],
            "message": parts[1] if len(parts) > 1 else "",
        })
    return commits

def _run_git_command(project_path, *git_args):
    """Run a small git command with a short timeout."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, *git_args],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        return None, "git command timed out after 2 seconds"
    except FileNotFoundError:
        return None, "git executable not available"
    except Exception as e:
        return None, f"git command failed: {e}"

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "git command failed").strip()
        return None, error or "git command failed"

    return result.stdout, None

def _decode_session_update_row(row):
    """Normalize a session_updates row for get_project_state output."""
    files_modified = _safe_json_loads(row["files_modified"], [])
    unexecuted_steps = _safe_json_loads(row["unexecuted_steps"], [])
    decisions = _safe_json_loads(row["decisions"], [])

    if not isinstance(files_modified, list):
        files_modified = []
    if not isinstance(unexecuted_steps, list):
        unexecuted_steps = []
    if not isinstance(decisions, list):
        decisions = []

    normalized_decisions = []
    for item in decisions[:5]:
        if isinstance(item, dict):
            normalized_decisions.append({
                "decision": item.get("decision", ""),
                "rationale": item.get("rationale", ""),
                "file": item.get("file", ""),
            })

    return {
        "created_at": row["created_at"],
        "task": row["task"],
        "files_modified": files_modified,
        "unexecuted_steps": unexecuted_steps[:5],
        "decisions": normalized_decisions,
        "unfinished": row["unfinished"] or "",
        "tokens_used": row["tokens_used"],
        "source": row["source"],
    }

def _resolve_live_project_path(project_name, stored_path):
    """Prefer a native Linux mirror under /home/dev/projects when available."""
    native_path = _to_native_path(stored_path)
    projects_root = "/home/dev/projects"
    if not native_path or not native_path.startswith("/mnt/") or not os.path.isdir(projects_root):
        return native_path

    try:
        entries = os.listdir(projects_root)
        for entry in entries:
            candidate = os.path.join(projects_root, entry)
            if entry == str(project_name) and os.path.isdir(candidate):
                return candidate
        for entry in entries:
            candidate = os.path.join(projects_root, entry)
            if entry.lower() == str(project_name).lower() and os.path.isdir(candidate):
                return candidate
    except OSError:
        pass

    return native_path

def _log_project_state_timing(project_name, **durations):
    """Emit one compact timing line for get_project_state diagnostics."""
    ordered = [f"{name}={value:.3f}s" for name, value in durations.items()]
    logging.getLogger("uvicorn.error").info(
        "[custodian] get_project_state timing project=%s %s",
        project_name,
        " ".join(ordered),
    )

async def handle_list_projects(args):
    with db_connection() as conn:
        rows = conn.execute(
            """SELECT p.name, p.path, p.stack, p.status, p.last_indexed,
                      COUNT(f.id) as fossil_count,
                      (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               GROUP BY p.id
               ORDER BY p.name"""
        ).fetchall()

    projects = []
    for row in rows:
        projects.append({
            "name": row["name"],
            "path": row["path"],
            "stack": row["stack"],
            "status": row["status"],
            "last_indexed": row["last_indexed"],
            "fossil_count": row["fossil_count"],
            "symbol_count": row["symbol_count"],
        })

    return [TextContent(type="text", text=json.dumps(projects, indent=2))]

async def handle_register_project(args):
    name = str(args.get("name") or "").strip()
    path = str(args.get("path") or "").strip()
    stack = str(args.get("stack") or "")
    status = str(args.get("status") or "active").strip() or "active"
    log_query("register_project", name or None, {**args, "path": path})

    if not name:
        return [TextContent(type="text", text="Error: 'name' is required.")]
    if not path:
        return [TextContent(type="text", text="Error: 'path' is required and must be a non-empty string.")]

    with db_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM projects WHERE LOWER(name) = LOWER(?)",
            (name,),
        ).fetchone()
        if existing:
            return [TextContent(type="text", text=f"Error: project '{name}' already exists.")]

        conn.execute(
            "INSERT INTO projects (name, path, stack, status) VALUES (?, ?, ?, ?)",
            (name, path, stack, status),
        )
        conn.commit()

        row = conn.execute(
            """SELECT p.name, p.path, p.stack, p.status, p.last_indexed,
                      COUNT(f.id) as fossil_count,
                      (SELECT COUNT(*) FROM symbols s WHERE s.project_id = p.id) as symbol_count
               FROM projects p
               LEFT JOIN fossils f ON f.project_id = p.id
               WHERE p.name = ?
               GROUP BY p.id""",
            (name,),
        ).fetchone()

    project = {
        "name": row["name"],
        "path": row["path"],
        "stack": row["stack"],
        "status": row["status"],
        "last_indexed": row["last_indexed"],
        "fossil_count": row["fossil_count"],
        "symbol_count": row["symbol_count"],
    }
    return [TextContent(type="text", text=json.dumps(project, indent=2))]

async def handle_get_fossil(args):
    project_name = args["project"]
    include_tree = args.get("include_file_tree", False)
    include_symbols = args.get("include_symbols", False)

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

        fossil = conn.execute(
            """SELECT * FROM fossils
               WHERE project_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (project["id"],),
        ).fetchone()

        if not fossil:
            return [TextContent(type="text", text=f"No fossil found for '{project_name}'. Run trigger_custodian to create one.")]

        result = {
            "project": project["name"],
            "path": project["path"],
            "stack": project["stack"],
            "fossil_version": fossil["version"],
            "fossil_date": fossil["created_at"],
            "summary": fossil["summary"],
            "architecture": fossil["architecture"],
            "known_issues": fossil["known_issues"],
            "dependencies": fossil["dependencies"],
        }

        if include_tree:
            result["file_tree"] = fossil["file_tree"]

        if include_symbols:
            symbols = conn.execute(
                """SELECT file_path, line_number, type, name, signature, description, relationships
                   FROM symbols WHERE fossil_id = ?
                   ORDER BY file_path, line_number""",
                (fossil["id"],),
            ).fetchall()
            result["symbols"] = [dict(s) for s in symbols]

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_project_state(args):
    project_name = args["project"]
    include_file_tree = args.get("include_file_tree", True)
    max_recent_commits = max(1, min(int(args.get("max_recent_commits", 10)), 50))
    overall_start = time.perf_counter()

    fossil_duration = 0.0
    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

        session_rows = conn.execute(
            """
            SELECT created_at, task, files_modified, unexecuted_steps, decisions, unfinished, tokens_used, source
            FROM session_updates
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 3
            """,
            (project["id"],),
        ).fetchall()

        box_row = conn.execute(
            """
            SELECT status, container_name, image, last_healthcheck
            FROM project_boxes
            WHERE project_id = ?
            """,
            (project["id"],),
        ).fetchone()

    result = {
        "project": project["name"],
        "path": project["path"],
        "stack": project["stack"],
        "status": project["status"],
        "last_indexed": project["last_indexed"],
        "box": {
            "status": box_row["status"],
            "container": box_row["container_name"],
            "image": box_row["image"],
            "last_healthcheck": box_row["last_healthcheck"],
        } if box_row else None,
    }

    if session_rows:
        result["recent_session_updates"] = [_decode_session_update_row(row) for row in session_rows]

    project_path = _resolve_live_project_path(project["name"], project["path"])
    if project_path != _to_native_path(project["path"]):
        result["resolved_path"] = project_path
    if not project_path:
        result["filesystem_accessible"] = False
        result["filesystem_reason"] = "Project path is empty."
        result["git_accessible"] = False
        result["git_reason"] = "Project path is empty."
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if not os.path.isdir(project_path):
        result["filesystem_accessible"] = False
        result["filesystem_reason"] = f"Resolved path is not accessible from this host: {project_path}"
        result["git_accessible"] = False
        result["git_reason"] = "Git unavailable because the project directory is not accessible."
        _log_project_state_timing(
            project_name,
            fossil=fossil_duration,
            filesystem=0.0,
            git_branch=0.0,
            git_status=0.0,
            git_log=0.0,
            session_updates=0.0,
            total=time.perf_counter() - overall_start,
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    result["filesystem_accessible"] = True
    filesystem_duration = 0.0
    if include_file_tree:
        filesystem_start = time.perf_counter()
        result["file_tree"] = _build_live_file_tree(
            project_path,
            {},
            max_entries=LIVE_FILE_TREE_ENTRY_LIMIT,
        )
        filesystem_duration = time.perf_counter() - filesystem_start

    session_updates_duration = 0.0

    git_dir = os.path.join(project_path, ".git")
    if not os.path.exists(git_dir):
        result["git_accessible"] = False
        result["git_reason"] = "Project is not a git repository."
        _log_project_state_timing(
            project_name,
            fossil=fossil_duration,
            filesystem=filesystem_duration,
            git_branch=0.0,
            git_status=0.0,
            git_log=0.0,
            session_updates=session_updates_duration,
            total=time.perf_counter() - overall_start,
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    git_branch_start = time.perf_counter()
    branch_stdout, branch_error = _run_git_command(project_path, "branch", "--show-current")
    git_branch_duration = time.perf_counter() - git_branch_start
    git_status_start = time.perf_counter()
    status_stdout, status_error = _run_git_command(project_path, "status", "--porcelain")
    git_status_duration = time.perf_counter() - git_status_start
    git_log_start = time.perf_counter()
    log_stdout, log_error = _run_git_command(project_path, "log", "--oneline", "-n", str(max_recent_commits))
    git_log_duration = time.perf_counter() - git_log_start

    if branch_stdout is None and status_stdout is None and log_stdout is None:
        result["git_accessible"] = False
        result["git_reason"] = branch_error or status_error or log_error or "Git metadata unavailable."
        _log_project_state_timing(
            project_name,
            fossil=fossil_duration,
            filesystem=filesystem_duration,
            git_branch=git_branch_duration,
            git_status=git_status_duration,
            git_log=git_log_duration,
            session_updates=session_updates_duration,
            total=time.perf_counter() - overall_start,
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    git_status = _parse_git_status(status_stdout or "") if status_stdout is not None else None
    commits = _parse_git_log(log_stdout or "") if log_stdout is not None else None

    result["git_accessible"] = True
    result["git"] = {
        "branch": branch_stdout.strip() if branch_stdout is not None else None,
        "status": git_status,
        "commits_since_fossil": commits,
        "uncommitted_changes": bool(git_status and any(git_status.values())) if git_status is not None else None,
    }
    if branch_error or status_error or log_error:
        result["git_errors"] = {
            "branch": branch_error,
            "status": status_error,
            "commits_since_fossil": log_error,
        }

    _log_project_state_timing(
        project_name,
        fossil=fossil_duration,
        filesystem=filesystem_duration,
        git_branch=git_branch_duration,
        git_status=git_status_duration,
        git_log=git_log_duration,
        session_updates=session_updates_duration,
        total=time.perf_counter() - overall_start,
    )

    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_setup_project_folder(args):
    project_name = str(args.get("project") or "").strip()
    log_query("setup_project_folder", project_name or None, args)

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]

    try:
        category = _validate_shared_category(args.get("category"))
    except ValueError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]

    include_mac = args.get("include_mac", False)
    if not isinstance(include_mac, bool):
        include_mac = str(include_mac).strip().lower() in {"1", "true", "yes"}

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

        existing = conn.execute(
            "SELECT * FROM project_folders WHERE project = ? AND category = ?",
            (project["name"], category),
        ).fetchone()
        if existing:
            result = {
                "project": existing["project"],
                "category": existing["category"],
                "wsl_path": existing["wsl_path"],
                "mac_path": existing["mac_path"],
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        wsl_path = os.path.join(_shared_project_root(project["name"]), category, "")
        mac_path = None
        mac_warning = None

        try:
            os.makedirs(wsl_path, exist_ok=True)
        except OSError as exc:
            return [TextContent(type="text", text=f"Error creating WSL shared folder: {exc}")]

        if include_mac:
            mac_path = f"/Users/tubslamanna/custodian-shared/{project['name']}/{category}/"
            result = _laptop_bridge_call(
                "laptop_run_command",
                {
                    "command": f"mkdir -p {shlex.quote(mac_path)}",
                    "cwd": "/Users/tubslamanna",
                    "timeout": 30,
                },
            )
            if isinstance(result, dict) and result.get("error"):
                mac_warning = result["error"]
                print(f"[custodian] setup_project_folder Mac warning: {mac_warning}", file=sys.stderr)

        conn.execute(
            """
            INSERT INTO project_folders (project, category, wsl_path, mac_path)
            VALUES (?, ?, ?, ?)
            """,
            (project["name"], category, wsl_path, mac_path),
        )
        conn.commit()

    result = {
        "project": project["name"],
        "category": category,
        "wsl_path": wsl_path,
        "mac_path": mac_path,
    }
    if mac_warning:
        result["mac_warning"] = mac_warning
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_get_project_folders(args):
    project_name = str(args.get("project") or "").strip()

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found. Use list_projects to see available projects.")]

        rows = conn.execute(
            """
            SELECT id, project, category, wsl_path, mac_path, created_at
            FROM project_folders
            WHERE project = ?
            ORDER BY category
            """,
            (project["name"],),
        ).fetchall()

    result = [dict(row) for row in rows]
    return [TextContent(type="text", text=json.dumps(result, indent=2))]

async def handle_request_reindex(args):
    """Create a pending reindex request for user approval in Admin TUI."""
    project_name = args.get("project", "")
    reason = args.get("reason", "")
    log_query("request_reindex", project_name, args)

    if not project_name:
        return [TextContent(type="text", text="Error: 'project' is required.")]
    if not reason:
        return [TextContent(type="text", text="Error: 'reason' is required.")]

    with db_connection() as conn:
        project = get_project_by_name(conn, project_name)
        if not project:
            return [TextContent(type="text", text=f"Project '{project_name}' not found.")]

        conn.execute(
            """INSERT INTO reindex_requests (project_id, requested_by, reason, status)
               VALUES (?, ?, ?, 'pending')""",
            (project["id"], "claude", reason),
        )
        conn.commit()

    return [TextContent(
        type="text",
        text=f"Reindex request created for '{project['name']}'. Awaiting user approval in Admin TUI.",
    )]



def _unwrap(result):
    text = result[0].text
    try:
        return json.loads(text)
    except Exception:
        return text


async def list_projects(conn, **params):
    return _unwrap(await handle_list_projects(params))


async def register_project(conn, **params):
    return _unwrap(await handle_register_project(params))


async def get_project_fossil(conn, **params):
    return _unwrap(await handle_get_fossil(params))


async def get_project_state(conn, **params):
    return _unwrap(await handle_get_project_state(params))


async def setup_project_folder(conn, **params):
    return _unwrap(await handle_setup_project_folder(params))


async def get_project_folders(conn, **params):
    return _unwrap(await handle_get_project_folders(params))


async def request_reindex(conn, **params):
    return _unwrap(await handle_request_reindex(params))
