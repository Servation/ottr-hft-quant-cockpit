import logging
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.api import router as api_router
from app.routers.discord_webhooks import router as discord_webhooks_router
from app.config import settings

# Setup standard logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("app.main")

# Instantiate FastAPI application
app = FastAPI(
    title="Python Agent Gateway",
    description="FastAPI proxy for the OTTR Discord Bridge",
    version="1.0.0"
)

# CORS: restrict to known dashboard origin(s). `allow_origins=["*"]` with
# credentials is both insecure and invalid per the CORS spec. Auth is via the
# X-API-Key header (not cookies), so credentials are disabled.
_cors_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Accept"],
)

# Include versioned API router
app.include_router(api_router, prefix="/api/v1")
app.include_router(discord_webhooks_router, prefix="/api/internal")


@app.on_event("startup")
async def startup_event():
    logger.info("Starting Python Agent Gateway...")
    logger.info(f"Default Locale: {settings.locale}")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Python Agent Gateway...")
