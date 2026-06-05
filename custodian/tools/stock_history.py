from __future__ import annotations

import json
import math
from typing import Any

from mcp.types import TextContent
import yfinance as yf


METADATA = {
    "name": "stock_history",
    "description": "Get historical price data (OHLCV) for a stock. Configurable period and interval. Returns bars plus summary stats (period high/low, change %). Use for trend and chart analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Single ticker symbol",
            },
            "period": {
                "type": "string",
                "description": "Time period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, max. Default: 1mo",
                "default": "1mo",
            },
            "interval": {
                "type": "string",
                "description": "Data interval: 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo. Default: 1d. Note: intraday intervals (1m-1h) only available for periods <= 7d.",
                "default": "1d",
            },
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


def _format_index(index_value: Any, interval: str) -> str:
    if hasattr(index_value, "to_pydatetime"):
        index_value = index_value.to_pydatetime()
    if hasattr(index_value, "strftime"):
        if interval in {"1m", "5m", "15m", "30m", "1h"}:
            return index_value.strftime("%Y-%m-%d %H:%M")
        return index_value.strftime("%Y-%m-%d")
    return str(index_value)


async def handle(params: dict, db):
    symbol = (params.get("symbol") or "").strip().upper()
    period = (params.get("period") or "1mo").strip()
    interval = (params.get("interval") or "1d").strip()
    if not symbol:
        return _json_text({"symbol": "", "period": period, "interval": interval, "error": "symbol is required"})

    try:
        history = yf.Ticker(symbol).history(period=period, interval=interval)
    except Exception as exc:
        return _json_text({"symbol": symbol, "period": period, "interval": interval, "error": str(exc)})

    if history.empty:
        return _json_text(
            {
                "symbol": symbol,
                "period": period,
                "interval": interval,
                "summary": {},
                "bars": [],
                "truncated": False,
                "error": "No historical data returned",
            }
        )

    history = history.dropna(subset=["Open", "High", "Low", "Close"])
    if history.empty:
        return _json_text(
            {
                "symbol": symbol,
                "period": period,
                "interval": interval,
                "summary": {},
                "bars": [],
                "truncated": False,
                "error": "No complete historical bars returned",
            }
        )

    first_open = _clean_number(history["Open"].iloc[0], 2)
    last_close = _clean_number(history["Close"].iloc[-1], 2)
    change_pct = None
    if first_open not in (None, 0) and last_close is not None:
        change_pct = round(((float(last_close) - float(first_open)) / float(first_open)) * 100, 2)

    total_volume = _clean_number(history["Volume"].fillna(0).sum()) if "Volume" in history else None
    summary = {
        "period_high": _clean_number(history["High"].max(), 2),
        "period_low": _clean_number(history["Low"].min(), 2),
        "period_open": first_open,
        "period_close": last_close,
        "period_change_pct": change_pct,
        "total_volume": total_volume,
        "bar_count": int(len(history)),
    }

    truncated = len(history) > 60
    output_history = history.tail(60) if truncated else history
    bars = []
    for index, row in output_history.iterrows():
        bars.append(
            {
                "date": _format_index(index, interval),
                "open": _clean_number(row.get("Open"), 2),
                "high": _clean_number(row.get("High"), 2),
                "low": _clean_number(row.get("Low"), 2),
                "close": _clean_number(row.get("Close"), 2),
                "volume": _clean_number(row.get("Volume")),
            }
        )

    return _json_text(
        {
            "symbol": symbol,
            "period": period,
            "interval": interval,
            "summary": summary,
            "bars": bars,
            "truncated": truncated,
        }
    )
