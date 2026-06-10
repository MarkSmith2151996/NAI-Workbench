from __future__ import annotations

import json
from mcp.types import TextContent
import os
from datetime import datetime

METADATA = {
    "name": "pull_keepa_download",
    "description": "List or read Keepa CSV downloads. Call with no arguments to list available files. Call with a filename to return its contents.",
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Name of a CSV file in the downloads folder. If omitted, returns a listing of all files with sizes and modified times."
            }
        },
        "required": []
    }
}


def _json_text(payload):
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def handle(params: dict, db):
    DOWNLOAD_DIR = "/home/dev/keepa-downloads"

    if not os.path.isdir(DOWNLOAD_DIR):
        return _json_text({"error": f"Download directory {DOWNLOAD_DIR} does not exist"})

    filename = params.get("filename")

    if not filename:
        files = []
        for f in sorted(os.listdir(DOWNLOAD_DIR)):
            filepath = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                files.append({
                    "name": f,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        if not files:
            return _json_text({"message": "No files in download directory", "path": DOWNLOAD_DIR})
        return _json_text({"files": files, "count": len(files)})
    else:
        safe_name = os.path.basename(filename)
        filepath = os.path.join(DOWNLOAD_DIR, safe_name)
        if not os.path.isfile(filepath):
            return _json_text({"error": f"File not found: {safe_name}", "available": os.listdir(DOWNLOAD_DIR)})
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        size_kb = round(os.path.getsize(filepath) / 1024, 1)
        return _json_text({"filename": safe_name, "size_kb": size_kb, "content": content})
