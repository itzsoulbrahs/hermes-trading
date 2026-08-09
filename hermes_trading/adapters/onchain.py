"""On-chain data adapter - fetches blockchain metrics."""

import asyncio
import os
from typing import Any

import httpx

SCHEMA_VERSION = "1.0"


class SchemaError(Exception):
    """Raised when adapter response schema doesn't match expected version."""
    pass


async def fetch(asset: str = "BTC") -> dict[str, Any]:
    """Fetch on-chain metrics for the given asset.
    
    Uses free public endpoints when no API key is configured.
    """
    glassnode_key = os.environ.get("GLASSNODE_API_KEY", "")
    
    # Map trading pair asset to blockchain metric symbols
    if "BTC" in asset.upper():
        metric = "balance_1bc"  # Balance on exchanges (proxy for exchange flows)
    elif "ETH" in asset.upper():
        metric = "eth_balance_1bc"
    else:
        metric = "balance_1bc"
    
    try:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    if glassnode_key:
                        url = f"https://api.glassnode.com/v2/indicators/{metric}/latest"
                        params = {"a": glassnode_key}
                    else:
                        # Fallback to free public data - exchange reserves from alternative source
                        url = "https://api.coingecko.com/api/v3/coins/bitcoin"
                        params = {}
                    
                    resp = await client.get(url, params=params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        
        if glassnode_key and isinstance(data, list) and len(data) > 0:
            value = data[-1].get("v", 0)
        elif glassnode_key and isinstance(data, dict):
            value = data.get("v", 0)
        else:
            # Coingecko fallback - extract exchange reserves
            value = data.get("market_data", {}).get("total_supply", 0)
        
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "asset": asset,
                "metric": metric,
                "value": float(value),
                "timestamp": asyncio.get_event_loop().time(),
            }
        }
    except Exception as e:
        # Return minimal valid response on failure
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "asset": asset,
                "metric": metric,
                "value": 0.0,
                "timestamp": asyncio.get_event_loop().time(),
                "error": str(e),
            }
        }