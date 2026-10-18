"""
Coinbase Exchange public ticker: the fallback quote source.

What it does: fetches bid/ask/last per product. Why: with Kraken as the only venue, a
Kraken outage would leave positions unmarked and protective exits blind. Coinbase is
only used when Kraken's quotes are stale, and fills record which venue priced them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Iterable, Optional

import httpx

from ottr.market.types import Quote

logger = logging.getLogger(__name__)

BASE = "https://api.exchange.coinbase.com"


async def quotes(assets: Iterable[str], client: Optional[httpx.AsyncClient] = None) -> Dict[str, Quote]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "ottr/2"})
    out: Dict[str, Quote] = {}
    try:
        for asset in assets:
            try:
                r = await client.get(f"{BASE}/products/{asset}-USD/ticker")
                r.raise_for_status()
                e = r.json()
                q = Quote(asset=asset, bid=float(e["bid"]), ask=float(e["ask"]),
                          last=float(e["price"]), ts=time.time(), source="coinbase")
                if q.valid():
                    out[asset] = q
            except (httpx.HTTPError, KeyError, ValueError) as err:
                logger.warning("Coinbase quote %s failed: %s", asset, err)
            await asyncio.sleep(0.15)  # stay well under the public rate limit
    finally:
        if own:
            await client.aclose()
    return out
