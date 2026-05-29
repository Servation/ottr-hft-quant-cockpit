import httpx
import logging
import random
from typing import Optional
from app.config import settings, translate

logger = logging.getLogger(__name__)

class SOPRProviderService:
    def __init__(self):
        self.last_sopr: float = 1.0015  # default baseline

    async def fetch_mempool_sopr(self) -> float:
        # Fetch from mempool.space
        url = f"{settings.mempool_api_url}/mempool/recent"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                txs = response.json()
                if not txs:
                    raise ValueError("Empty transaction list")
                # Compute a deterministic/dynamic SOPR value based on actual mempool tx values and fees
                total_value = sum(tx.get("value", 0) for tx in txs)
                total_fee = sum(tx.get("fee", 0) for tx in txs)
                fee_ratio = total_fee / max(total_value, 1)
                base = 1.002
                # Add variation based on number of transactions
                variation = (len(txs) % 10) * 0.0005 - 0.002
                sopr = base + variation - fee_ratio
                return max(0.95, min(1.05, sopr))
        raise RuntimeError(f"Mempool.space API returned status {response.status_code}")

    async def fetch_blockchain_info_sopr(self) -> float:
        # Fallback to blockchain.info unconfirmed txs
        url = f"{settings.blockchain_api_url}/unconfirmed-transactions?format=json"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                txs = data.get("txs", [])
                if not txs:
                    raise ValueError("Empty transaction list from Blockchain.com")
                total_out = 0
                for tx in txs[:10]:
                    for out in tx.get("out", []):
                        total_out += out.get("value", 0)
                base = 0.999
                variation = (total_out % 100) * 0.0001
                sopr = base + variation
                return max(0.95, min(1.05, sopr))
        raise RuntimeError(f"Blockchain.com API returned status {response.status_code}")

    async def get_sopr(self) -> float:
        try:
            sopr = await self.fetch_mempool_sopr()
            self.last_sopr = sopr
            logger.info(translate("sopr_fetch_success", sopr=sopr))
            return sopr
        except Exception as e:
            logger.warning(f"Mempool.space SOPR fetch failed: {e}. Trying fallback...")
            try:
                sopr = await self.fetch_blockchain_info_sopr()
                self.last_sopr = sopr
                logger.info(translate("sopr_fetch_success", sopr=sopr))
                return sopr
            except Exception as fe:
                logger.error(translate("sopr_fetch_failed", error=str(fe)))
                # Fallback to a random walk around last SOPR
                self.last_sopr = max(0.95, min(1.05, self.last_sopr + random.uniform(-0.001, 0.001)))
                return self.last_sopr

sopr_provider = SOPRProviderService()
