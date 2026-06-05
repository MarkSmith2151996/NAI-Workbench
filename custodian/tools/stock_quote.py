from __future__ import annotations

import json
import math
from typing import Any

from mcp.types import TextContent
import yfinance as yf


METADATA = {
    "name": "stock_quote",
    "description": "Get live stock quotes for one or more ticker symbols. Returns current price, change, volume, and day range. Pass comma-separated symbols like 'MRVL,TSM,ONTO'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "string",
                "description": "Comma-separated ticker symbols (e.g., 'MRVL,TSM,ONTO,CAMT')",
            }
        },
        "required": ["symbols"],
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


def _get(mapping: Any, *keys: str) -> Any:
    for key in keys:
        try:
            value = mapping.get(key)
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def _history_fallback(ticker: yf.Ticker) -> tuple[float | None, float | None]:
    try:
        history = ticker.history(period="2d", interval="1d")
        if history.empty:
            return None, None
        close = history["Close"].dropna()
        if close.empty:
            return None, None
        price = _clean_number(close.iloc[-1], 2)
        previous_close = _clean_number(close.iloc[-2], 2) if len(close) > 1 else None
        return price, previous_close
    except Exception:
        return None, None


def _quote_for_symbol(symbol: str) -> dict[str, Any]:
    quote: dict[str, Any] = {"symbol": symbol}
    try:
        ticker = yf.Ticker(symbol)
        fast_info = ticker.fast_info

        price = _clean_number(_get(fast_info, "lastPrice", "last_price"), 2)
        previous_close = _clean_number(
            _get(fast_info, "previousClose", "regularMarketPreviousClose", "previous_close"), 2
        )
        open_price = _clean_number(_get(fast_info, "open"), 2)
        day_high = _clean_number(_get(fast_info, "dayHigh", "day_high"), 2)
        day_low = _clean_number(_get(fast_info, "dayLow", "day_low"), 2)
        volume = _clean_number(_get(fast_info, "lastVolume", "volume"))
        market_cap = _clean_number(_get(fast_info, "marketCap", "market_cap"))

        if price is None or previous_close is None:
            fallback_price, fallback_previous = _history_fallback(ticker)
            price = price if price is not None else fallback_price
            previous_close = previous_close if previous_close is not None else fallback_previous

        if any(value is None for value in (price, previous_close, open_price, day_high, day_low, volume, market_cap)):
            try:
                info = ticker.info
            except Exception:
                info = {}
            price = price if price is not None else _clean_number(
                _get(info, "currentPrice", "regularMarketPrice"), 2
            )
            previous_close = previous_close if previous_close is not None else _clean_number(
                _get(info, "previousClose", "regularMarketPreviousClose"), 2
            )
            open_price = open_price if open_price is not None else _clean_number(_get(info, "open"), 2)
            day_high = day_high if day_high is not None else _clean_number(
                _get(info, "dayHigh", "regularMarketDayHigh"), 2
            )
            day_low = day_low if day_low is not None else _clean_number(
                _get(info, "dayLow", "regularMarketDayLow"), 2
            )
            volume = volume if volume is not None else _clean_number(_get(info, "volume", "regularMarketVolume"))
            market_cap = market_cap if market_cap is not None else _clean_number(_get(info, "marketCap"))

        change = None
        change_pct = None
        if price is not None and previous_close not in (None, 0):
            change = round(float(price) - float(previous_close), 2)
            change_pct = round((change / float(previous_close)) * 100, 2)

        quote.update(
            {
                "price": price,
                "previous_close": previous_close,
                "change": change,
                "change_pct": change_pct,
                "open": open_price,
                "day_high": day_high,
                "day_low": day_low,
                "volume": volume,
                "market_cap": market_cap,
            }
        )
        if price is None:
            quote["error"] = "No quote data returned"
        return quote
    except Exception as exc:
        quote["error"] = str(exc)
        return quote


async def handle(params: dict, db):
    symbols_raw = (params.get("symbols") or "").strip()
    symbols = [symbol.strip().upper() for symbol in symbols_raw.split(",") if symbol.strip()]
    if not symbols:
        return _json_text({"quotes": [], "count": 0, "error": "symbols is required"})

    quotes = [_quote_for_symbol(symbol) for symbol in symbols]
    return _json_text({"quotes": quotes, "count": len(quotes)})
