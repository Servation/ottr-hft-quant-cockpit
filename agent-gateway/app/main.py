import logging
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.routers.api import router as api_router
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
    description="Microservice managing trading analysts and gateway execution in the Polyglot Trading Bot",
    version="1.0.0"
)

# Setup CORS middleware for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include versioned API router
app.include_router(api_router, prefix="/api/v1")

# Instrument Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Python Agent Gateway...")
    logger.info(f"Default Locale: {settings.locale}")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Python Agent Gateway...")
