from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

from mcp.types import TextContent
import yfinance as yf


METADATA = {
    "name": "stock_details",
    "description": "Get detailed stock information for a single ticker: market cap, P/E, 52-week range, earnings date, analyst targets, sector, and more. Use for research before entering a position.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Single ticker symbol (e.g., 'ONTO')",
            }
        },
        "required": ["symbol"],
    },
}


def _json_text(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _clean_number(value: Any, ndigits: int | None = None) -> float | int | None:
    try:
        if value is None:
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        if ndigits is not None:
            number = round(number, ndigits)
        return int(number) if ndigits is None and number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _format_large_number(value: Any, prefix: str = "") -> str | None:
    number = _clean_number(value)
    if number is None:
        return None
    abs_number = abs(float(number))
    for suffix, divisor in (("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs_number >= divisor:
            return f"{prefix}{float(number) / divisor:.1f}{suffix}"
    return f"{prefix}{number:,}"


def _format_percent(value: Any) -> float | None:
    number = _clean_number(value, 4)
    if number is None:
        return None
    return round(number * 100, 2) if abs(number) <= 0.2 else round(number, 2)


def _format_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value) if value else None


def _get_next_earnings_date(ticker: yf.Ticker, info: dict[str, Any]) -> str | None:
    for key in ("earningsTimestamp", "earningsTimestampStart", "earningsDate"):
        formatted = _format_timestamp(info.get(key))
        if formatted:
            return formatted
    try:
        calendar = ticker.calendar
        if calendar is None:
            return None
        if hasattr(calendar, "empty") and not calendar.empty:
            values = calendar.to_numpy().flatten().tolist()
        elif isinstance(calendar, dict):
            values = list(calendar.values())
        else:
            values = []
        for value in values:
            formatted = _format_timestamp(value)
            if formatted:
                return formatted
    except Exception:
        return None
    return None


async def handle(params: dict, db):
    symbol = (params.get("symbol") or "").strip().upper()
    if not symbol:
        return _json_text({"symbol": "", "error": "symbol is required"})

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
    except Exception as exc:
        return _json_text({"symbol": symbol, "error": str(exc)})

    if not info:
        return _json_text({"symbol": symbol, "error": "No stock details returned"})

    payload = {
        "symbol": symbol,
        "company_name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": _format_large_number(info.get("marketCap"), "$"),
        "market_cap_raw": _clean_number(info.get("marketCap")),
        "current_price": _clean_number(info.get("currentPrice") or info.get("regularMarketPrice"), 2),
        "fifty_two_week_high": _clean_number(info.get("fiftyTwoWeekHigh"), 2),
        "fifty_two_week_low": _clean_number(info.get("fiftyTwoWeekLow"), 2),
        "trailing_pe": _clean_number(info.get("trailingPE"), 2),
        "forward_pe": _clean_number(info.get("forwardPE"), 2),
        "trailing_eps": _clean_number(info.get("trailingEps"), 2),
        "earnings_date": _get_next_earnings_date(ticker, info),
        "dividend_yield": _format_percent(info.get("dividendYield")),
        "average_volume_10_day": _format_large_number(info.get("averageVolume10days")),
        "average_volume_10_day_raw": _clean_number(info.get("averageVolume10days")),
        "average_volume_3_month": _format_large_number(info.get("averageVolume")),
        "average_volume_3_month_raw": _clean_number(info.get("averageVolume")),
        "beta": _clean_number(info.get("beta"), 2),
        "short_interest": _format_large_number(info.get("sharesShort")),
        "short_interest_raw": _clean_number(info.get("sharesShort")),
        "short_ratio": _clean_number(info.get("shortRatio"), 2),
        "analyst_target_mean": _clean_number(info.get("targetMeanPrice"), 2),
        "analyst_target_low": _clean_number(info.get("targetLowPrice"), 2),
        "analyst_target_high": _clean_number(info.get("targetHighPrice"), 2),
        "analyst_count": _clean_number(info.get("numberOfAnalystOpinions")),
        "analyst_recommendation": info.get("recommendationKey"),
        "analyst_recommendation_mean": _clean_number(info.get("recommendationMean"), 2),
    }
    if payload["company_name"] is None and payload["current_price"] is None and payload["market_cap_raw"] is None:
        payload["error"] = "No stock details returned"
    return _json_text(payload)
