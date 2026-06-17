import aiohttp
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://localhost:8000/api/internal/discord-sync"

async def send_gateway_event(event_name: str, data: Any):
    """
    Sends an event payload to the FastAPI gateway.
    """
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "event": event_name,
                "data": data
            }
            async with session.post(GATEWAY_URL, json=payload) as response:
                if response.status != 200:
                    logger.warning(f"Failed to sync event {event_name} to gateway. Status: {response.status}")
    except Exception as e:
        logger.error(f"Error syncing event {event_name} to gateway: {e}")

async def sync_agent_state(agent_states: list):
    await send_gateway_event("agent_state", agent_states)

async def sync_portfolio(portfolio_state: dict):
    await send_gateway_event("portfolio", portfolio_state)

async def sync_execution(execution_log: dict):
    await send_gateway_event("execution", execution_log)
