import asyncio
import json
import logging
from typing import AsyncGenerator, Dict, Any
from app.config import settings, translate

logger = logging.getLogger(__name__)

class SSEManager:
    def __init__(self):
        # Store active listener queues
        self.queues: list[asyncio.Queue] = []

    async def subscribe(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Subscribes a new client to the SSE event stream.
        Yields events as they are broadcasted.
        """
        queue = asyncio.Queue()
        self.queues.append(queue)
        client_id = id(queue)
        
        # Log client connection in correct locale
        logger.info(translate("sse_client_connected", client_id=client_id))
        
        try:
            while True:
                # Wait for next event
                event_data = await queue.get()
                yield event_data
        except asyncio.CancelledError:
            # Client disconnected
            logger.info(translate("sse_client_disconnected", client_id=client_id))
        finally:
            if queue in self.queues:
                self.queues.remove(queue)

    async def broadcast(self, event_name: str, data: Dict[str, Any]):
        """
        Broadcasts an event to all connected clients.
        """
        payload = {
            "event": event_name,
            "data": json.dumps(data)
        }
        for queue in self.queues:
            await queue.put(payload)

sse_manager = SSEManager()
