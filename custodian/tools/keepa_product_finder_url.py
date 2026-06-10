from __future__ import annotations

import json
from mcp.types import TextContent
import json
import urllib.parse
from datetime import datetime

METADATA = {
    "name": "keepa_product_finder_url",
    "description": "Generate Keepa Product Finder URLs for one or more brand names. Returns properly formatted URLs using the verified working format (t=g, lowercase brand, no restrictive filters). Supports single brand or batch mode.",
    "input_schema": {
        "type": "object",
        "properties": {
            "brand_name": {
                "type": "string",
                "description": "Single brand name to generate URL for. Use this OR brand_names, not both."
            },
            "brand_names": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "List of brand names for batch URL generation. Use this OR brand_name, not both."
            }
        }
    }
}


def _json_text(payload):
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def handle(params: dict, db):
    brand_name = params.get("brand_name")
    brand_names = params.get("brand_names")

    if not brand_name and not brand_names:
        return _json_text({"error": "Provide brand_name (single) or brand_names (list)"})

    # Normalize to list
    names = brand_names if brand_names else [brand_name]

    # Current month for srAvgMonth filter
    now = datetime.utcnow()
    sr_month = now.strftime("%Y%m")

    results = []
    for name in names:
        # Brand name must be lowercase for Keepa autocomplete
        brand_lower = name.strip().lower()

        # Verified working JSON structure (Memory #168)
        # "t":"g" is REQUIRED — "t":"f" silently returns zero results
        # productType and srAvgMonth are required fields
        # No price/FBA/drops filters — let analyzer handle filtering downstream
        finder_json = {
            "t": "g",
            "f": {
                "brandStoreName": {
                    "filterType": "autocomplete",
                    "filter": brand_lower,
                    "type": "isOneOf"
                },
                "productType": {
                    "values": ["0"],
                    "filterType": "set"
                },
                "srAvgMonth": {
                    "filterType": "text",
                    "type": "equals",
                    "filter": sr_month
                }
            },
            "s": [
                {"colId": "SALES_current", "sort": "asc"},
                {"colId": "monthlySold", "sort": "desc"}
            ]
        }

        # Compact JSON, then URL-encode
        compact = json.dumps(finder_json, separators=(",", ":"))
        encoded = urllib.parse.quote(compact)
        url = f"https://keepa.com/#!finder/{encoded}"

        results.append({
            "brand_name": name.strip(),
            "brand_name_normalized": brand_lower,
            "keepa_url": url
        })

    if brand_name and not brand_names:
        # Single mode — return flat object
        return _json_text(results[0])
    else:
        # Batch mode — return list with count
        return _json_text({"count": len(results), "urls": results})
