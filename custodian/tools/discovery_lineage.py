from __future__ import annotations

import json
from mcp.types import TextContent
import sys

METADATA = {
    "name": "discovery_lineage",
    "description": "Log and query FBA discovery lineage events, including direct scans, seller ecosystem discoveries, downstream seed lookup, contamination flags, and summary stats.",
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "log_scan",
                    "log_ecosystem",
                    "flag_contamination",
                    "get_downstream",
                    "get_lineage",
                    "stats"
                ]
            },
            "brand_name": {
                "type": "string"
            },
            "source_lake": {
                "type": "string"
            },
            "keepa_url": {
                "type": "string"
            },
            "seed_asin": {
                "type": "string"
            },
            "seed_brand": {
                "type": "string"
            },
            "seller_id": {
                "type": "string"
            },
            "seller_name": {
                "type": "string"
            },
            "upstream_chain": {
                "type": "array",
                "items": {
                    "type": "object"
                }
            },
            "reason": {
                "type": "string"
            }
        },
        "required": [
            "operation"
        ]
    }
}


def _json_text(payload):
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def handle(params: dict, db):
    repo_root = "/home/dev/projects/ComandAndControl"
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from validation import lineage_logger

    operation = params.get("operation")

    if operation == "log_scan":
        brand_name = params.get("brand_name")
        source_lake = params.get("source_lake")
        if not brand_name or not source_lake:
            return _json_text({"ok": False, "error": "log_scan requires brand_name and source_lake"})
        entry = lineage_logger.log_direct_scan(
            brand_name=brand_name,
            source_lake=source_lake,
            keepa_url=params.get("keepa_url", ""),
        )
        return _json_text({"ok": True, "entry": entry})

    if operation == "log_ecosystem":
        required = ["brand_name", "source_lake", "seed_asin", "seed_brand", "seller_id"]
        missing = [field for field in required if not params.get(field)]
        if missing:
            return _json_text({"ok": False, "error": f"log_ecosystem missing required fields: {', '.join(missing)}"})
        entry = lineage_logger.log_ecosystem_discovery(
            brand_name=params["brand_name"],
            source_lake=params["source_lake"],
            seed_asin=params["seed_asin"],
            seed_brand=params["seed_brand"],
            seller_id=params["seller_id"],
            seller_name=params.get("seller_name", ""),
            upstream_chain=params.get("upstream_chain") or [],
            keepa_url=params.get("keepa_url", ""),
        )
        return _json_text({"ok": True, "entry": entry})

    if operation == "flag_contamination":
        seed_asin = params.get("seed_asin")
        if not seed_asin:
            return _json_text({"ok": False, "error": "flag_contamination requires seed_asin"})
        flagged = lineage_logger.flag_contamination(seed_asin, params.get("reason", "invalidated"))
        return _json_text({"ok": True, "flagged": flagged, "seed_asin": seed_asin})

    if operation == "get_downstream":
        seed_asin = params.get("seed_asin")
        if not seed_asin:
            return _json_text({"ok": False, "error": "get_downstream requires seed_asin"})
        brands = lineage_logger.get_downstream_brands(seed_asin)
        return _json_text({"ok": True, "seed_asin": seed_asin, "brands": brands, "count": len(brands)})

    if operation == "get_lineage":
        brand_name = params.get("brand_name")
        if not brand_name:
            return _json_text({"ok": False, "error": "get_lineage requires brand_name"})
        entries = lineage_logger.get_lineage(brand_name)
        return _json_text({"ok": True, "brand_name": brand_name, "entries": entries, "count": len(entries)})

    if operation == "stats":
        return _json_text({"ok": True, "stats": lineage_logger.stats()})

    return _json_text({"ok": False, "error": f"unknown operation: {operation}"})
