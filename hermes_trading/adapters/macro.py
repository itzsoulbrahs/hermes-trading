"""Macro data adapter - fetches macroeconomic indicators."""

import asyncio
from typing import Any

import httpx
import yfinance as yf

SCHEMA_VERSION = "1.0"


class SchemaError(Exception):
    """Raised when adapter response schema doesn't match expected version."""
    pass


async def fetch() -> dict[str, Any]:
    """Fetch macroeconomic indicators relevant for crypto trading.
    
    Includes:
    - DXY (US Dollar Index)
    - VIX (Volatility Index)
    - US 10Y Treasury Yield
    """
    try:
        for attempt in range(3):
            try:
                # Use yfinance for free macro data
                tickers = ["DX-Y.NYB", "VIX", "TNX"]
                data = {}
                
                for ticker in tickers:
                    t = yf.Ticker(ticker)
                    hist = t.history(period="5d")
                    if not hist.empty:
                        data[ticker] = float(hist["Close"].iloc[-1])
                    else:
                        data[ticker] = 0.0
                
                break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        
        dxy = data.get("DX-Y.NYB", 0)
        vix = data.get("VIX", 0)
        tnx = data.get("TNX", 0)
        
        # Compute risk-on/risk-off signal
        # High DXY = strong dollar = typically bearish for crypto
        # High VIX = fear = typically bearish
        risk_signal = 0.5  # neutral
        
        if dxy > 0 and vix > 0:
            # Simple heuristic: normalize and combine
            dxy_signal = max(0, min(1, (105 - dxy) / 10))  # Below 105 = risk-on
            vix_signal = max(0, min(1, (15 - vix) / 10))   # Below 15 = risk-on
            risk_signal = (dxy_signal + vix_signal) / 2
        
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "dxy": dxy,
                "vix": vix,
                "us_10y_yield": tnx,
                "risk_signal": float(risk_signal),
                "timestamp": asyncio.get_event_loop().time(),
            }
        }
    except Exception as e:
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "dxy": 0.0,
                "vix": 0.0,
                "us_10y_yield": 0.0,
                "risk_signal": 0.5,
                "timestamp": asyncio.get_event_loop().time(),
                "error": str(e),
            }
        }