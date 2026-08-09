"""Price data adapter - fetches OHLCV data from exchanges via ccxt."""

import asyncio
import os
from typing import Any

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

SCHEMA_VERSION = "1.0"


class SchemaError(Exception):
    """Raised when adapter response schema doesn't match expected version."""
    pass


async def fetch(symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 100) -> dict[str, Any]:
    """Fetch OHLCV price data from exchange.
    
    Returns dict with schema_version and data dict containing:
    - symbol: trading pair
    - timeframe: candle timeframe
    - ohlcv: list of [timestamp, open, high, low, close, volume]
    - indicators: computed technical indicators
    """
    api_key = os.environ.get("EXCHANGE_API_KEY", "")
    api_secret = os.environ.get("EXCHANGE_API_SECRET", "")
    
    exchange = ccxt.binance(config={
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
    })
    
    try:
        for attempt in range(3):
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        
        # Compute RSI
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
        df["rsi"] = df["rsi"].fillna(50)
        
        # Compute SMA
        df["sma_20"] = df["close"].rolling(window=20).mean()
        df["sma_50"] = df["close"].rolling(window=50).mean()
        
        latest = df.iloc[-1]
        
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": int(latest["timestamp"].timestamp()),
                "open": float(latest["open"]),
                "high": float(latest["high"]),
                "low": float(latest["low"]),
                "close": float(latest["close"]),
                "volume": float(latest["volume"]),
                "rsi": float(latest["rsi"]),
                "sma_20": float(latest["sma_20"]),
                "sma_50": float(latest["sma_50"]),
                "ohlcv": ohlcv,
            }
        }
    finally:
        await exchange.close()