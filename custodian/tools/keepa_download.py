from __future__ import annotations

import json
from mcp.types import TextContent
from custodian.services.native import call_extension

METADATA = {
    "name": "keepa_download",
    "description": "Download Keepa Product Finder CSVs via the keepa-downloader service. Calls the native extension, returns download result with rows/path. The Mac file watcher automatically transfers CSVs to the fba-command-center box \u2014 use keepa_poll to find them.",
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "Keepa Product Finder URLs to download"
            },
            "brand": {
                "type": "string",
                "description": "Brand name for logging"
            }
        },
        "required": [
            "urls"
        ]
    }
}


async def handle(params: dict, db):
    urls = params.get("urls", [])
    brand = params.get("brand", "unknown")
    
    if not urls:
        return [TextContent(type="text", text=json.dumps({"error": "No URLs provided"}))]
    
    result = call_extension(
        "keepa-downloader",
        "/download",
        method="POST",
        data={"urls": urls},
        timeout=120,
    )
    
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
