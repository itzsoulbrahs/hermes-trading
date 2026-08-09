"""News sentiment adapter - fetches crypto news sentiment."""

import asyncio
import os
from typing import Any

import httpx

SCHEMA_VERSION = "1.0"


class SchemaError(Exception):
    """Raised when adapter response schema doesn't match expected version."""
    pass


async def fetch(query: str = "bitcoin") -> dict[str, Any]:
    """Fetch news sentiment data.
    
    Uses free public endpoints when no API key is configured.
    """
    news_api_key = os.environ.get("NEWS_API_KEY", "")
    
    try:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    if news_api_key:
                        url = "https://newsapi.org/v2/everything"
                        params = {
                            "q": query,
                            "sortBy": "popularity",
                            "language": "en",
                            "apiKey": news_api_key,
                            "pageSize": 10,
                        }
                    else:
                        # Fallback to CryptoPanic free API
                        url = "https://cryptopanic.com/api/v1/posts/"
                        params = {"public": "true", "currencies": query[:3].upper()}
                    
                    resp = await client.get(url, params=params, timeout=30)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    else:
                        data = {"articles": [], "results": []}
                        break
            except Exception as e:
                if attempt == 2:
                    data = {"articles": [], "results": []}
                    break
                await asyncio.sleep(2 ** attempt)
        
        # Extract sentiment score from headlines (simple keyword-based)
        articles = data.get("articles", data.get("results", []))
        bullish_keywords = ["bull", "rise", "gain", "up", "rally", "breakout", "positive"]
        bearish_keywords = ["bear", "drop", "loss", "down", "crash", "sell", "negative"]
        
        sentiment_score = 0.5  # neutral default
        if articles:
            bullish_count = 0
            bearish_count = 0
            for article in articles[:10]:
                title = article.get("title", "").lower()
                bullish_count += sum(1 for kw in bullish_keywords if kw in title)
                bearish_count += sum(1 for kw in bearish_keywords if kw in title)
            
            total = bullish_count + bearish_count
            if total > 0:
                sentiment_score = bullish_count / total
        
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "query": query,
                "sentiment_score": float(sentiment_score),
                "article_count": len(articles),
                "timestamp": asyncio.get_event_loop().time(),
            }
        }
    except Exception as e:
        return {
            "schema_version": SCHEMA_VERSION,
            "data": {
                "query": query,
                "sentiment_score": 0.5,
                "article_count": 0,
                "timestamp": asyncio.get_event_loop().time(),
                "error": str(e),
            }
        }