from __future__ import annotations

import json
from mcp.types import TextContent
import importlib
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

METADATA = {
    "name": "keepa_competition_analyzer",
    "description": "Analyze a downloaded Keepa brand CSV using competition and velocity only. Accepts csv_path or brand_name and returns pass/fail counts, velocity grades, fail reasons, and stamped params_used.",
    "input_schema": {
        "type": "object",
        "properties": {
            "csv_path": {
                "type": "string",
                "description": "Full path to a Keepa CSV file."
            },
            "brand_name": {
                "type": "string",
                "description": "Brand name to resolve as /home/dev/keepa-downloads/{safe_brand_name}.csv."
            },
            "min_fba_sellers": {
                "type": "integer",
                "description": "Minimum FBA seller count. Defaults to 2."
            },
            "max_fba_sellers": {
                "type": "integer",
                "description": "Crowded-listing flag threshold. Defaults to 15."
            },
            "min_price": {
                "type": "number",
                "description": "Minimum buy box price. Defaults to 15.0."
            },
            "max_amazon_pct": {
                "type": "number",
                "description": "Maximum Amazon buy box share as 0.0-1.0. Defaults to 0.35."
            },
            "max_top_seller_pct_small_field": {
                "type": "number",
                "description": "Maximum top seller buy box share for listings with <=4 FBA sellers. Defaults to 0.60."
            },
            "max_top_seller_pct": {
                "type": "number",
                "description": "Maximum top seller buy box share for listings with >=5 FBA sellers as 0.0-1.0. Defaults to 0.70."
            },
            "velocity_a_threshold": {
                "type": "integer",
                "description": "Monthly unit threshold for A velocity. Defaults to 300."
            },
            "velocity_b_threshold": {
                "type": "integer",
                "description": "Monthly unit threshold for B velocity. Defaults to 120."
            },
            "velocity_c_threshold": {
                "type": "integer",
                "description": "Monthly unit threshold for C velocity. Defaults to 50."
            },
            "filter_gated": {
                "type": "boolean",
                "description": "Whether to fail gated/blocked products. Defaults to true."
            }
        },
        "required": []
    }
}


def _json_text(payload):
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def handle(params: dict, db):
    PROJECT_ROOT = Path("/home/dev/projects/ComandAndControl")
    VALIDATION_DIR = PROJECT_ROOT / "validation"
    DOWNLOAD_DIR = Path("/home/dev/keepa-downloads")

    csv_path = (params.get("csv_path") or "").strip()
    brand_name = (params.get("brand_name") or "").strip()

    for path in [str(PROJECT_ROOT), str(VALIDATION_DIR)]:
        if path not in sys.path:
            sys.path.insert(0, path)

    try:
        import grading_function
        importlib.reload(grading_function)
        import competition_grading
        competition_grading = importlib.reload(competition_grading)
        CompetitionParams = competition_grading.CompetitionParams
        analyze_csv = competition_grading.analyze_csv
    except Exception as exc:
        return _json_text({"error": f"Failed to import competition grading dependencies: {type(exc).__name__}: {exc}"})

    def get_int(name, default):
        try:
            value = params.get(name, default)
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(name, default):
        try:
            value = params.get(name, default)
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(name, default):
        value = params.get(name, default)
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    competition_params = CompetitionParams(
        min_price=get_float("min_price", 15.0),
        min_fba_sellers=get_int("min_fba_sellers", 2),
        max_fba_sellers=get_int("max_fba_sellers", 15),
        filter_gated=get_bool("filter_gated", True),
        max_amazon_pct=get_float("max_amazon_pct", 0.35),
        max_top_seller_pct_small_field=get_float("max_top_seller_pct_small_field", 0.60),
        max_top_seller_pct=get_float("max_top_seller_pct", 0.70),
        velocity_a_threshold=get_int("velocity_a_threshold", 300),
        velocity_b_threshold=get_int("velocity_b_threshold", 120),
        velocity_c_threshold=get_int("velocity_c_threshold", 50),
    )
    params_used = asdict(competition_params)

    if not csv_path and not brand_name:
        return _json_text({"error": "csv_path or brand_name is required", "params_used": params_used})

    if csv_path:
        resolved_csv = Path(csv_path).expanduser()
    else:
        safe_name = re.sub(r"[^\w\-.]", "_", brand_name)
        resolved_csv = DOWNLOAD_DIR / f"{safe_name}.csv"

    resolved_csv = resolved_csv.resolve()
    if not resolved_csv.is_file():
        available = []
        if DOWNLOAD_DIR.is_dir():
            available = sorted(p.name for p in DOWNLOAD_DIR.glob("*.csv"))
        return _json_text({"error": f"CSV not found: {resolved_csv}", "available": available, "params_used": params_used})

    start = time.time()
    try:
        result = analyze_csv(resolved_csv, brand=brand_name or resolved_csv.stem.replace("_", " "), params=competition_params)
    except Exception as exc:
        return _json_text({"error": f"Analysis failed: {type(exc).__name__}: {exc}", "csv_path": str(resolved_csv), "params_used": params_used})

    result["runtime_s"] = round(time.time() - start, 3)
    result["summary"] = f"{result['brand']}: {result['pass_count']} pass, {result['fail_count']} fail across {result['total_products']} products"
    return _json_text(result)
