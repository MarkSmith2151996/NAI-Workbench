from __future__ import annotations

import json
from mcp.types import TextContent
import os
import sys
import time
import math
import re
from pathlib import Path

METADATA = {
    "name": "keepa_brand_analyzer",
    "description": "Analyze a downloaded Keepa brand CSV using the in-repo Windows validation kit grading logic. Accepts csv_path or brand_name and returns pass/maybe/fail counts plus passing product details.",
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
            "max_price": {
                "type": "number",
                "description": "Maximum product price filter. Defaults to 500."
            }
        },
        "required": []
    }
}


def _json_text(payload):
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def handle(params: dict, db):
    VALIDATION_DIR = Path("/home/dev/projects/ComandAndControl/validation")
    DOWNLOAD_DIR = Path("/home/dev/keepa-downloads")

    csv_path = (params.get("csv_path") or "").strip()
    brand_name = (params.get("brand_name") or "").strip()
    try:
        max_price = float(params.get("max_price", 500) or 500)
    except (TypeError, ValueError):
        max_price = 500.0

    if not csv_path and not brand_name:
        return _json_text({"error": "csv_path or brand_name is required"})

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
        return _json_text({"error": f"CSV not found: {resolved_csv}", "available": available})

    if not VALIDATION_DIR.is_dir():
        return _json_text({"error": f"Validation directory not found: {VALIDATION_DIR}"})

    start = time.time()
    if str(VALIDATION_DIR) not in sys.path:
        sys.path.insert(0, str(VALIDATION_DIR))

    try:
        import pandas as pd
        from grading_function import GradingParams, evaluate_prepared, prepare_dataframe
    except Exception as exc:
        return _json_text({"error": f"Failed to import validation kit dependencies: {type(exc).__name__}: {exc}"})

    try:
        raw_df = None
        parse_error = None
        for sep in ["\t", ",", ";"]:
            try:
                candidate = pd.read_csv(resolved_csv, sep=sep, dtype=str, low_memory=False)
                if len(candidate.columns) > 1:
                    raw_df = candidate
                    break
            except Exception as exc:
                parse_error = exc
        if raw_df is None:
            return _json_text({"error": f"Could not parse CSV: {parse_error}"})

        params_obj = GradingParams(
            max_price=max_price,
            max_fba_sellers=15,
            min_drops_90d=30,
            min_monthly_units=40,
            min_your_units=5,
            max_top_seller_pct=1.00,
            hard_reject_pct=1.00,
            cogs_pct=0.55,
            apply_bb_share_realism=True,
            min_realistic_profit=0,
            min_winners_90d=0,
            skip_topseller_demotion=True,
        )
        prepared = prepare_dataframe(raw_df)
        details = evaluate_prepared(prepared, params_obj)
    except Exception as exc:
        return _json_text({"error": f"Analysis failed: {type(exc).__name__}: {exc}", "csv_path": str(resolved_csv)})

    brand = brand_name or resolved_csv.stem.replace("_", " ")
    pass_mask = details["verdict"] == "PASS"
    maybe_mask = details["verdict"] == "MAYBE"
    fail_mask = details["verdict"] == "FAIL"

    passing_products = []
    for idx, row in details[pass_mask].iterrows():
        title = ""
        try:
            title = prepared.loc[idx, "Title"]
        except Exception:
            title = ""
        def clean(value):
            try:
                if value is None:
                    return None
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    return None
                if hasattr(value, "item"):
                    value = value.item()
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    return None
                return value
            except Exception:
                return str(value)
        passing_products.append({
            "asin": clean(row.get("ASIN")),
            "title": clean(title),
            "price": clean(row.get("price")),
            "estimated_profit": clean(row.get("monthly_profit")),
            "realistic_profit": clean(row.get("realistic_profit")),
            "roi": clean(row.get("roi")),
            "grade": clean(row.get("grade")),
            "reason": clean(row.get("reason")),
        })

    runtime_s = round(time.time() - start, 3)
    pass_count = int(pass_mask.sum())
    maybe_count = int(maybe_mask.sum())
    fail_count = int(fail_mask.sum())
    summary = f"{brand}: {pass_count} pass, {maybe_count} maybe, {fail_count} fail across {len(raw_df)} products"

    return _json_text({
        "brand": brand,
        "csv_path": str(resolved_csv),
        "max_price": max_price,
        "blocked_brands_file": str(VALIDATION_DIR / "blocked_brands.txt"),
        "total_products": int(len(raw_df)),
        "pass_count": pass_count,
        "maybe_count": maybe_count,
        "fail_count": fail_count,
        "passing_products": passing_products,
        "runtime_s": runtime_s,
        "summary": summary,
    })
