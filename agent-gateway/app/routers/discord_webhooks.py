from fastapi import APIRouter, Request, HTTPException
import logging
from app.services.sse_manager import sse_manager

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/discord-sync")
async def discord_sync(request: Request):
    """
    Receives state updates from the Discord bot and broadcasts them to the React frontend via SSE.
    """
    try:
        payload = await request.json()
        event_type = payload.get("event")
        data = payload.get("data")

        if not event_type or data is None:
            raise HTTPException(status_code=400, detail="Missing 'event' or 'data' in payload")

        # Valid events expected by the frontend: agent_state, execution, portfolio
        if event_type in ["agent_state", "execution", "portfolio"]:
            await sse_manager.broadcast(event_type, data)
            logger.info(f"Broadcasted Discord event: {event_type}")
        else:
            logger.warning(f"Received unknown event type from Discord bot: {event_type}")

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing discord sync webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
